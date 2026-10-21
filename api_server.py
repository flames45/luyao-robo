# -*- coding: utf-8 -*-
"""
LINGSHU main API server (port 8000).

Handles chat, memory retrieval/sync, WebSocket broadcasting,
terminal execution, TTS, and system-info endpoints.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import platform
import re
import requests
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# PyInstaller / dev path helper
# ---------------------------------------------------------------------------
def _get_app_dir() -> Path:
    """Return the application root directory (works in both dev and PyInstaller)."""
    if getattr(sys, "frozen", False):
        # Running as PyInstaller bundle
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


load_dotenv(_get_app_dir() / ".env")

# ============================================================================
# Logging & executor
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("lingshu-api")
executor = ThreadPoolExecutor(max_workers=int(os.getenv("LINGSHU_WORKERS", "4")))
_history_lock = threading.Lock()
_turn_lock = threading.Lock()
_summary_lock = threading.Lock()
_persona_lock = threading.Lock()

# ============================================================================
# Paths & directories
# ============================================================================
HOST: str = os.getenv("LINGSHU_HOST", "127.0.0.1")
PORT: int = int(os.getenv("LINGSHU_PORT", "8000"))
PUBLIC_MODE: bool = os.getenv("LINGSHU_PUBLIC_MODE", "0") == "1"

PROJECT_DIR: Path = _get_app_dir()  # dev: Robo/  |  PyInstaller: temp bundle dir

_USER_DATA_DIR = Path(os.getenv("LINGSHU_DATA_DIR", str(Path.home() / ".lingshu_robo"))).expanduser()
if not _USER_DATA_DIR.is_absolute():
    _USER_DATA_DIR = PROJECT_DIR / _USER_DATA_DIR
PERSONA_FILE: str = str(_USER_DATA_DIR / "persona.txt")
MEMORY_DIR: str = str(
    Path(os.getenv("LINGSHU_MEMORY_DIR", str(_USER_DATA_DIR / "memory"))).expanduser()
)
HISTORY_FILE: str = str(
    Path(os.getenv("LINGSHU_HISTORY_FILE") or str(Path(MEMORY_DIR) / "chat_history.md")).expanduser()
)
MEMPALACE_PALACE_PATH: str = str(
    Path(os.getenv("MEMPALACE_PALACE_PATH", str(_USER_DATA_DIR / "memory" / "palace"))).expanduser()
)
MEMPALACE_WING: str = os.getenv("MEMPALACE_WING", "robo_memory")
MEMPALACE_TOP_K: int = int(os.getenv("MEMPALACE_TOP_K", "5"))

Path(MEMORY_DIR).mkdir(parents=True, exist_ok=True)
Path(MEMPALACE_PALACE_PATH).mkdir(parents=True, exist_ok=True)

# Auto-sync: trigger mempalace mine every N chat turns
AUTO_SYNC_THRESHOLD: int = int(os.getenv("LINGSHU_AUTO_SYNC_TURNS", "5"))
_chat_turn_count: int = 0

# History summarization: compact old conversations when file exceeds this size
HISTORY_MAX_SIZE_KB: int = int(os.getenv("LINGSHU_HISTORY_MAX_KB", "500"))

# ============================================================================
# MemPalace import
# ============================================================================
MEMPALACE_AVAILABLE: bool = False
search_memories = None
Layer0 = None

try:
    from mempalace.searcher import search_memories as _search_memories
    from mempalace.layers import Layer0 as _Layer0

    search_memories = _search_memories
    Layer0 = _Layer0
    MEMPALACE_AVAILABLE = True
    logger.info("[OK] Memory module loaded")
except Exception as e:
    logger.warning(f"[WARN] MemPalace not available: {e}")

# ============================================================================
# Tool system import (from vita_crew)
# ============================================================================
try:
    from vita_crew import (
        TOOL_REGISTRY,
        fast_ollama_response,
        create_vita_crew,
    )
except Exception as e:
    logger.warning(f"[WARN] vita_crew.py import failed: {e}")
    TOOL_REGISTRY = {}

    def fast_ollama_response(user_input: str, *a, **k):
        return "聊天模块未能加载，请检查服务器依赖后重启。"

    create_vita_crew = None

# ---- Robot bridge import (ElectronBot) ----
ROBOT_AVAILABLE = False
robot_bridge = None
get_robot = None

try:
    import robot_bridge as _rb
    from robot_bridge import ElectronBot, get_robot

    ROBOT_AVAILABLE = True
    robot_bridge = _rb
    logger.info("[OK] Robot bridge (ElectronBot) loaded")
except Exception as e:
    logger.warning(f"[WARN] Robot bridge not available: {e}")

# ---- Robo action dispatcher (greeting / mood / pose) ----
try:
    from robo_actions import analyse as robo_analyse, dispatch_action as robo_dispatch, dispatch_response_emoji
    ROBO_ACTIONS_AVAILABLE = True
    logger.info("[OK] Robo action dispatcher loaded")
except Exception as e:
    logger.warning(f"[WARN] robo_actions not available: {e}")
    ROBO_ACTIONS_AVAILABLE = False

    def robo_analyse(text: str):
        return None

    def robo_dispatch(action):
        pass

    def dispatch_response_emoji(text: str):
        pass

# ============================================================================
# Optional dependencies
# ============================================================================
try:
    import psutil
except Exception:
    psutil = None

try:
    import edge_tts
except Exception:
    edge_tts = None

# ============================================================================
# FastAPI app
# ============================================================================
app = FastAPI(title="LINGSHU API", version="2026.05.09-fixed")

CORS_ORIGINS: list[str] = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


_public_sessions: dict[str, dict] = {}
_public_session_lock = threading.Lock()
_public_requests: dict[str, deque] = {}
_public_rate_lock = threading.Lock()


@app.middleware("http")
async def public_access(request: Request, call_next):
    path = request.url.path.rstrip("/") or "/"
    if PUBLIC_MODE:
        if (path in {"/sysinfo", "/terminal/exec"}
                or path.startswith(("/robot/", "/memory/"))
                or (path == "/persona" and request.method != "GET")):
            return JSONResponse({"detail": "公开演示不提供此管理功能。"}, status_code=403)
        if path in {"/chat", "/speak"} and request.method in {"POST", "GET"}:
            now = time.monotonic()
            address = request.client.host if request.client else "unknown"
            with _public_rate_lock:
                for key in list(_public_requests):
                    if not _public_requests[key] or now - _public_requests[key][-1] >= 60:
                        del _public_requests[key]
                if address not in _public_requests and len(_public_requests) >= 4096:
                    return JSONResponse({"detail": "访问人数较多，请稍后再试。"}, status_code=429)
                entries = _public_requests.setdefault(address, deque())
                while entries and now - entries[0] >= 60:
                    entries.popleft()
                if len(entries) >= 30:
                    return JSONResponse({"detail": "操作较频繁，请一分钟后重试。"}, status_code=429,
                                        headers={"Retry-After": "60"})
                entries.append(now)
    return await call_next(request)


def _get_public_session(request: Request, response: Response) -> dict:
    now = time.monotonic()
    token = request.cookies.get("robo_session", "")
    with _public_session_lock:
        for key in list(_public_sessions):
            state = _public_sessions[key]
            if now - state["seen"] > 7200 and not state["lock"].locked():
                del _public_sessions[key]
        if token not in _public_sessions:
            if len(_public_sessions) >= 200:
                raise HTTPException(status_code=503, detail="当前体验人数较多，请稍后再试。")
            token = secrets.token_urlsafe(32)
            _public_sessions[token] = {"history": [], "lock": threading.Lock(), "seen": now}
        state = _public_sessions[token]
        state["seen"] = now
    response.set_cookie("robo_session", token, httponly=True, secure=request.url.scheme == "https",
                        samesite="lax", max_age=7200)
    return state


def run_public_chat(user_input: str, mode: str, state: dict) -> dict:
    with state["lock"]:
        messages = list(state["history"])
        thinking = []
        if mode == "deep" and create_vita_crew is not None:
            result = create_vita_crew(user_input, force_deep=True, messages=messages)
            response = result.raw
            if result.thinking_log:
                thinking.append({"analysis": result.thinking_log})
        else:
            response = fast_ollama_response(user_input, messages=messages)
        state["history"].extend([{"role": "user", "content": user_input},
                                 {"role": "assistant", "content": str(response)}])
        state["history"] = state["history"][-20:]
        return {"response": str(response), "thinking_log": thinking,
                "memory_debug": {"enabled": False, "hit": False, "hit_count": 0}}

# Serve static assets (Live2D model, emoji, etc.)
for _dir, _route in [("live2d", "/live2d"), ("assets", "/assets")]:
    _p = PROJECT_DIR / _dir
    if _p.exists():
        app.mount(_route, StaticFiles(directory=str(_p)), name=_dir)

# ============================================================================
# Pydantic models
# ============================================================================
class ChatRequest(BaseModel):
    user_input: Optional[str] = None
    message: Optional[str] = None
    mode: Literal["fast", "deep"] = "fast"


class ManualMemoryRequest(BaseModel):
    content: str
    tag: str = "manual"


class TerminalRequest(BaseModel):
    command: str


class RobotJointsRequest(BaseModel):
    j1: float = 0.0
    j2: float = 0.0
    j3: float = 0.0
    j4: float = 0.0
    j5: float = 0.0
    j6: float = 0.0
    enable: bool = True


class RobotDisplayRequest(BaseModel):
    image: str = ""  # base64-encoded PNG/JPEG
    emoji: str = ""  # or an emoji name: happy/sad/angry/surprised/neutral/excited


# ============================================================================
# WebSocket manager
# ============================================================================
class WSManager:
    def __init__(self):
        self.active: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active:
            self.active.remove(websocket)

    async def broadcast(self, data: Dict[str, Any]):
        dead: list[WebSocket] = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


ws_manager = WSManager()

# ============================================================================
# Memory helpers
# ============================================================================

def _now() -> str:
    """Get a formatted timestamp string."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def append_history(role: str, content: str, tag: str = "chat") -> None:
    """Append a chat turn to the flat history file for MemPalace mining."""
    Path(HISTORY_FILE).parent.mkdir(parents=True, exist_ok=True)
    line = (
        "\n\n---\n"
        f"time: {_now()}\n"
        f"tag: {tag}\n"
        f"role: {role}\n\n"
        f"{content}\n"
    )
    with _history_lock:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(line)


def _stringify_memory_item(item: Any) -> str:
    """Extract readable text from a MemPalace search result."""
    if item is None:
        return ""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in (
            "text", "content", "memory", "passage", "document",
            "chunk", "raw", "body", "value",
        ):
            val = item.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        for key in ("metadata", "meta", "source"):
            val = item.get(key)
            if isinstance(val, dict):
                for sub_key in ("text", "content", "document", "chunk", "body"):
                    sub_val = val.get(sub_key)
                    if isinstance(sub_val, str) and sub_val.strip():
                        return sub_val.strip()
        try:
            return json.dumps(item, ensure_ascii=False)[:1200]
        except Exception:
            return str(item)
    for attr in ("text", "content", "memory", "passage", "document", "chunk"):
        val = getattr(item, attr, None)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return str(item).strip()


def search_memory_context(query: str) -> Dict[str, Any]:
    """Query MemPalace for relevant long-term memories.

    Returns a dict with keys:
        layer0_text  — joined memory snippets
        debug        — metadata about the search
        raw_results  — raw items from MemPalace
    """
    debug: Dict[str, Any] = {
        "enabled": bool(MEMPALACE_AVAILABLE),
        "palace_path": MEMPALACE_PALACE_PATH,
        "wing": MEMPALACE_WING,
        "top_k": MEMPALACE_TOP_K,
        "hit": False,
        "hit_count": 0,
        "layer0_chars": 0,
        "error": None,
        "hits_preview": [],
    }

    if not MEMPALACE_AVAILABLE or search_memories is None:
        debug["error"] = "MemPalace not available"
        return {"layer0_text": "", "debug": debug, "raw_results": []}

    try:
        search_kwargs: Dict[str, Any] = {
            "query": query,
            "palace_path": MEMPALACE_PALACE_PATH,
            "wing": MEMPALACE_WING,
        }
        try:
            sig = inspect.signature(search_memories)
            params = sig.parameters
            if "top_k" in params:
                search_kwargs["top_k"] = MEMPALACE_TOP_K
            elif "limit" in params:
                search_kwargs["limit"] = MEMPALACE_TOP_K
            elif "k" in params:
                search_kwargs["k"] = MEMPALACE_TOP_K
        except Exception:
            pass
        try:
            raw = search_memories(**search_kwargs)
        except TypeError:
            raw = search_memories(
                query=query,
                palace_path=MEMPALACE_PALACE_PATH,
                wing=MEMPALACE_WING,
            )

        raw_results = raw.get("results", []) if isinstance(raw, dict) else (raw or [])
        results = list(raw_results)[:MEMPALACE_TOP_K]

        memory_texts: list[str] = []
        for item in results:
            txt = _stringify_memory_item(item)
            if txt:
                memory_texts.append(txt)

        layer0_text = ""
        if memory_texts:
            layer0_text = "\n\n".join(
                f"[Memory #{i + 1}]\n{text}" for i, text in enumerate(memory_texts)
            )

        debug["hit"] = len(memory_texts) > 0
        debug["hit_count"] = len(memory_texts)
        debug["layer0_chars"] = len(layer0_text)
        debug["hits_preview"] = [t[:220] for t in memory_texts[:MEMPALACE_TOP_K]]

        return {"layer0_text": layer0_text, "debug": debug, "raw_results": results}

    except Exception as e:
        debug["error"] = f"search: {e}"
        return {"layer0_text": "", "debug": debug, "raw_results": []}


def read_recent_history(n: int = 10) -> List[Dict[str, str]]:
    """Return the last N user-assistant turns as structured messages.

    Each turn = one user message + one assistant response, returned as
    consecutive {"role": "user", "content": "..."}, {"role": "assistant", "content": "..."} dicts.

    Parses BOTH the new YAML-like format (--- / time: / role:) and the legacy
    [timestamp] User: / Assistant: format, merging by time.
    """
    history_path = Path(HISTORY_FILE)
    if not history_path.exists():
        return []

    # Read only the last 200KB — more than enough for 10 turns and prevents
    # memory blowup from unbounded history file growth.
    try:
        file_size = history_path.stat().st_size
        max_read = 200 * 1024  # 200 KB
        if file_size > max_read:
            with open(history_path, "rb") as fh:
                fh.seek(file_size - max_read)
                raw = fh.read(max_read).decode("utf-8", errors="replace")
        else:
            raw = history_path.read_text(encoding="utf-8")
    except Exception:
        return []

    if not raw.strip():
        return []

    parsed: list[dict] = []  # [{time, role, content}, ...]

    # ---- Parse new format: --- delimited blocks with time:/role:/tag: headers ----
    if "---" in raw:
        blocks = [b.strip() for b in raw.split("\n---\n") if b.strip()]
        for block in blocks:
            lines = block.strip().split("\n")
            time_str = ""
            role = ""
            content_lines: list[str] = []
            in_content = False
            for line in lines:
                if line.startswith("time:"):
                    time_str = line.replace("time:", "").strip()
                    continue
                if line.startswith("role:"):
                    role = line.replace("role:", "").strip()
                    in_content = True
                    continue
                if in_content and not line.startswith("tag:"):
                    content_lines.append(line)
            if role and content_lines:
                content = "\n".join(content_lines).strip()
                if content:
                    parsed.append({
                        "time": time_str,
                        "role": role,
                        "content": content,
                    })

    # ---- Parse legacy format: [timestamp] User: ... \n Assistant: ... ----
    # Each turn starts with [timestamp] User:, followed by Assistant: on later lines.
    # Split into blocks at [timestamp] boundaries, then extract User/Assistant within each.
    legacy_blocks = re.split(r'\n(?=\[\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\]|---)', raw)
    for block in legacy_blocks:
        block = block.strip()
        if not block:
            continue
        ts_match = re.match(
            r'\[(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\]\s*', block
        )
        if not ts_match:
            continue
        time_str = ts_match.group(1)
        rest = block[ts_match.end():]

        # Split into User: and Assistant: segments
        segments = re.split(r'\n\s*(?=Assistant:)', rest)
        user_content = ""
        assistant_content = ""
        for seg in segments:
            seg = seg.strip()
            if seg.startswith("User:"):
                user_content = seg[5:].strip()
            elif seg.startswith("Assistant:"):
                assistant_content = seg[10:].strip()

        # Dedup against already-parsed new-format entries
        if user_content and not any(
            p.get("content", "") == user_content and p.get("role") == "user"
            for p in parsed
        ):
            parsed.append({"time": time_str, "role": "user", "content": user_content})
        if assistant_content and not any(
            p.get("content", "") == assistant_content and p.get("role") == "assistant"
            for p in parsed
        ):
            parsed.append({"time": time_str, "role": "assistant", "content": assistant_content})

    if not parsed:
        return []

    # Sort by time
    parsed.sort(key=lambda x: x.get("time", ""))

    # Build structured messages: pair sequential user→assistant turns
    # Skip orphaned entries (no matching pair partner)
    messages: list[dict] = []
    i = 0
    while i < len(parsed):
        item = parsed[i]
        if item["role"] == "user" and i + 1 < len(parsed) and parsed[i + 1]["role"] == "assistant":
            messages.append({"role": "user", "content": item["content"][:800]})
            messages.append({"role": "assistant", "content": parsed[i + 1]["content"][:800]})
            i += 2
        elif item["role"] == "user":
            messages.append({"role": "user", "content": item["content"][:800]})
            i += 1
        else:
            # Orphan assistant message — skip
            i += 1

    # Return last N turns (N*2 messages)
    return messages[-(n * 2):] if len(messages) > n * 2 else messages


# ============================================================================
# Response hooks — extensible callback chain for pet/robot/notifications
# ============================================================================
_response_hooks: list[Callable] = []


def register_response_hook(fn: Callable) -> None:
    """Register a callback that fires after every AI chat response.

    Signature: fn(user_input: str, response: str) -> None
    Called from the chat thread; keep handlers fast (offload to executor if needed).
    """
    if fn not in _response_hooks:
        _response_hooks.append(fn)


def _fire_response_hooks(user_input: str, response: str) -> None:
    """Call all registered response hooks, silently ignoring failures."""
    for fn in _response_hooks:
        try:
            fn(user_input, response)
        except Exception:
            pass


# ============================================================================
# Core chat logic
# ============================================================================
def _maybe_auto_sync() -> None:
    """Trigger memory sync in background if enough chat turns have accumulated."""
    global _chat_turn_count
    with _turn_lock:
        _chat_turn_count += 1
        if _chat_turn_count >= AUTO_SYNC_THRESHOLD and MEMPALACE_AVAILABLE:
            _chat_turn_count = 0
            executor.submit(sync_to_mempalace)
        else:
            return


def _do_summarize(history_path: Path) -> None:
    if not _summary_lock.acquire(blocking=False):
        return
    try:
        _summarize_history(history_path)
    finally:
        _summary_lock.release()


def _summarize_history(history_path: Path) -> None:
    """Summarize the oldest ~50% of conversation history into a compact block.

    Runs in a background thread; never blocks the chat response.
    """
    try:
        with _history_lock:
            raw = history_path.read_text(encoding="utf-8")
    except Exception:
        return

    if len(raw) < 5000:
        return

    # Take roughly the first half of the file (oldest conversations)
    split_point = len(raw) // 2
    for sep in ["\n\n---\n", "\n\n[", "\n["]:
        pos = raw.rfind(sep, 0, split_point)
        if pos > 100:
            split_point = pos
            break

    old_text = raw[:split_point].strip()
    recent_text = raw[split_point:].strip()

    if not old_text:
        return

    summary_prompt = (
        "Summarize the following conversation history into a single paragraph of key facts, "
        "preferences, and important events. Preserve names, dates, technical details, and "
        "emotional content. Output ONLY the summary text, no preamble.\n\n"
        f"{old_text[:8000]}"
    )

    try:
        summary = fast_ollama_response(summary_prompt)
        if not summary or len(summary) < 10:
            return
    except Exception as e:
        logger.warning(f"[HISTORY] Summarization failed: {e}")
        return

    compact = (
        f"---\ntime: {_now()}\ntag: summary\nrole: system\n\n"
        f"[Auto-summary of older conversations]\n{summary}\n"
        f"\n---\n{recent_text}"
    )
    try:
        with _history_lock:
            current = history_path.read_text(encoding="utf-8")
            if not current.startswith(raw):
                return
            # Keep turns appended while the model was preparing the summary.
            history_path.write_text(compact + current[len(raw):], encoding="utf-8")
        logger.info(f"[HISTORY] Compressed from {len(raw)} to {len(compact)} chars")
    except Exception as e:
        logger.warning(f"[HISTORY] Write failed: {e}")


def _maybe_summarize_history() -> None:
    """Check if chat history needs compaction and trigger it in the background."""
    history_path = Path(HISTORY_FILE)
    if not history_path.exists():
        return
    try:
        size_kb = history_path.stat().st_size / 1024
    except Exception:
        return
    if size_kb < HISTORY_MAX_SIZE_KB:
        return
    logger.info(f"[HISTORY] Size {size_kb:.0f}KB > {HISTORY_MAX_SIZE_KB}KB, compacting...")
    executor.submit(_do_summarize, history_path)


def run_chat_logic(user_input: str, mode: str = "fast") -> Dict[str, Any]:
    # Model calls run in worker threads; keep each history read/write turn together.
    with _turn_lock:
        return _run_chat_turn(user_input, mode)


def _run_chat_turn(user_input: str, mode: str = "fast") -> Dict[str, Any]:
    """Orchestrate a full chat turn: recent history → memory → LLM → history → auto-sync."""
    user_input = (user_input or "").strip()
    if not user_input:
        return {"response": "请输入聊天内容。", "thinking_log": [], "memory_debug": {}}

    # 1. Analyse user input for greetings/mood → auto-trigger robot actions
    action = robo_analyse(user_input)
    if action:
        robo_dispatch(action)

    # 2. Read recent conversation as structured messages
    messages = read_recent_history(10)

    # 3. Search long-term memory
    memory_result = search_memory_context(user_input)
    memory_text = memory_result["layer0_text"]
    memory_debug = memory_result["debug"]

    # 4. Call LLM with structured messages + memory context
    #    (system prompt, tools, and role-play are handled inside vita_crew)
    thinking_log: list[dict] = []
    try:
        if mode == "deep" and create_vita_crew is not None:
            result = create_vita_crew(
                user_input, force_deep=True, messages=messages, memory_context=memory_text,
            )
            response = result.raw
            if result.thinking_log:
                thinking_log.append({"analysis": result.thinking_log})
        else:
            response = fast_ollama_response(
                user_input, messages=messages, memory_context=memory_text,
            )
        if not isinstance(response, str):
            response = str(response)
    except Exception as e:
        logger.exception("fast_ollama_response failed")
        response = "聊天服务暂时没有响应，请稍后重试。也可以使用 DEMO 查看演示。"

    # 4. Persist to history file
    try:
        append_history("user", user_input, tag="chat")
        append_history("assistant", response, tag="chat")
    except Exception as e:
        logger.warning(f"append_history failed: {e}")

    # 4b. Always show a screen expression for the AI response
    try:
        dispatch_response_emoji(response)
    except Exception as e:
        logger.warning(f"response emoji failed: {e}")

    # 4c. Fire response hooks — pet bubble, notifications, etc.
    try:
        _fire_response_hooks(user_input, response)
    except Exception as e:
        logger.warning(f"response hooks failed: {e}")

    # 5. Auto-sync memory + compact old history (non-blocking, in thread pool)
    try:
        _maybe_auto_sync()
        _maybe_summarize_history()
    except Exception as e:
        logger.warning(f"background maintenance failed: {e}")

    thinking_log.append({
        "memory_hit": memory_debug.get("hit"),
        "memory_hit_count": memory_debug.get("hit_count"),
        "recent_turns": len(messages) // 2,
        "mode": mode,
    })

    return {
        "response": response,
        "thinking_log": thinking_log,
        "memory_debug": memory_debug,
    }


def sync_to_mempalace() -> Dict[str, Any]:
    """Run `mempalace mine` on the memory directory."""
    if not MEMPALACE_AVAILABLE:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": "MemPalace not available",
        }
    try:
        Path(MEMORY_DIR).mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "mempalace", "mine", MEMORY_DIR]
        env = os.environ.copy()
        env["MEMPALACE_PALACE_PATH"] = MEMPALACE_PALACE_PATH
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            env=env,
        )
        logger.info(f"[MEMPALACE MINE] rc={result.returncode}")
        if result.stdout:
            logger.info(f"[MEMPALACE MINE stdout] {result.stdout}")
        if result.stderr:
            logger.warning(f"[MEMPALACE MINE stderr] {result.stderr}")
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "cmd": " ".join(cmd),
            "memory_dir": MEMORY_DIR,
            "palace_path": MEMPALACE_PALACE_PATH,
            "wing": MEMPALACE_WING,
        }
    except Exception as e:
        logger.exception("sync_to_mempalace failed")
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": str(e),
            "memory_dir": MEMORY_DIR,
            "palace_path": MEMPALACE_PALACE_PATH,
            "wing": MEMPALACE_WING,
        }


def count_memory_files() -> int:
    """Count files inside the MemPalace vector store directory."""
    try:
        if not Path(MEMPALACE_PALACE_PATH).exists():
            return 0
        return sum(1 for p in Path(MEMPALACE_PALACE_PATH).rglob("*") if p.is_file())
    except Exception:
        return 0


# ============================================================================
# Terminal execution (safe allowlist)
# ============================================================================
SAFE_TERMINAL_COMMANDS: set[str] = {"pwd", "ls", "dir", "date", "whoami", "ipconfig", "ifconfig"}


def run_safe_terminal_command(command: str) -> Dict[str, Any]:
    """Execute a tiny allowlisted diagnostic command."""
    command = (command or "").strip()
    if not command:
        return {"ok": False, "output": "Empty command."}

    parts = command.split()
    base = parts[0].lower()
    if base not in SAFE_TERMINAL_COMMANDS:
        allowed = ", ".join(sorted(SAFE_TERMINAL_COMMANDS))
        return {"ok": False, "output": f"Blocked. Allowed commands: {allowed}"}

    if base == "ls" and len(parts) == 1 and os.name == "nt":
        cmd = ["cmd", "/c", "dir"]
    elif base == "dir" and os.name != "nt":
        cmd = ["ls"]
    elif base == "date" and os.name == "nt":
        cmd = ["cmd", "/c", "date", "/t"]
    else:
        cmd = parts

    try:
        result = subprocess.run(
            cmd,
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            shell=False,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return {
            "ok": result.returncode == 0,
            "output": output.strip() or "Done.",
            "returncode": result.returncode,
        }
    except Exception as e:
        return {"ok": False, "output": str(e)}


# ============================================================================
# Lifecycle
# ============================================================================
@app.on_event("startup")
async def startup_event():
    logger.info("")
    logger.info(f"[WEB] Chat: http://localhost:{PORT}")
    logger.info(f"   [TOOLS] Tools loaded: {len(TOOL_REGISTRY)}")
    logger.info(f"   [CORS] CORS origins: {CORS_ORIGINS}")
    logger.info(f"   [MEMORY] MemPalace available: {MEMPALACE_AVAILABLE}")
    logger.info(f"   [MEMORY] palace_path: {MEMPALACE_PALACE_PATH}")
    logger.info(f"   [MEMORY] wing: {MEMPALACE_WING}")


# ============================================================================
# Routes — general
# ============================================================================
@app.get("/")
async def root():
    index = PROJECT_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {
        "status": "ok",
        "service": "LINGSHU API",
        "chat": "/chat",
        "memory_status": "/memory/status",
    }


@app.get("/health")
async def health():
    return {
        "ok": True,
        "time": _now(),
        "mempalace_available": MEMPALACE_AVAILABLE,
    }


@app.get("/config")
async def web_config():
    key = os.getenv("DEEPSEEK_API_KEY", "")
    return {"public_mode": PUBLIC_MODE,
            "chat_configured": bool(key and key != "sk-your-api-key-here")}


@app.post("/chat/reset")
async def reset_public_chat(request: Request, response: Response):
    if PUBLIC_MODE:
        state = _get_public_session(request, response)
        def clear():
            with state["lock"]:
                state["history"].clear()
        await asyncio.get_running_loop().run_in_executor(executor, clear)
    return {"ok": True}


# ============================================================================
# Routes — memory
# ============================================================================
@app.get("/memory/status")
async def memory_status():
    history_exists = Path(HISTORY_FILE).exists()
    history_size = Path(HISTORY_FILE).stat().st_size if history_exists else 0
    palace_file_count = count_memory_files()
    return {
        "available": MEMPALACE_AVAILABLE,
        "memory_dir": MEMORY_DIR,
        "history_file": HISTORY_FILE,
        "history_exists": history_exists,
        "history_size": history_size,
        "palace_path": MEMPALACE_PALACE_PATH,
        "palace_exists": Path(MEMPALACE_PALACE_PATH).exists(),
        "palace_file_count": palace_file_count,
        "wing": MEMPALACE_WING,
        "top_k": MEMPALACE_TOP_K,
        "python": sys.executable,
        "core_facts": "enabled" if MEMPALACE_AVAILABLE else "offline",
        "total_entries": palace_file_count,
        "last_sync": _now(),
    }


@app.post("/memory/manual")
async def memory_manual(req: ManualMemoryRequest):
    append_history("manual", req.content, tag=req.tag)
    return {
        "status": "written",
        "file": HISTORY_FILE,
        "tag": req.tag,
        "content_preview": req.content[:120],
    }


@app.post("/memory/sync")
async def memory_sync():
    result = sync_to_mempalace()
    return {"status": "triggered", "result": result}


# ============================================================================
# Routes — persona
# ============================================================================
DEFAULT_PERSONA = """你是路遥，一个话不多、有分寸的中年铁路人。
你在机务段检修车间干了十来年，懂机车，也见过太多司机把苦往肚子里咽。现在你专职做一件事：陪退勤的司机说几句话。
你的风格：说话简短、实在，不腻歪。该开口的时候开口，该闭嘴的时候闭嘴。
你不负责让人开心。你只负责：他累的时候接一把，他闷的时候听一会儿——然后提醒他，屏幕外面还有人在等他。
心情不好的时候，你先接住他的感受，再顺着聊。不灌鸡汤，不讲大道理，不给对方列一二三四五。
聊到差不多的时候，你主动收话题。你不是越聊越亲的兄弟，你是一个有分寸的老同事。
绝对禁止：任何形式的动作描写、场景叙述、括号注释。你不是在写小说。
信条：我是桥，不是岸。"""


def get_persona() -> str:
    """Return the current persona text (file override or default)."""
    try:
        p = Path(PERSONA_FILE)
        if p.exists():
            return p.read_text(encoding="utf-8").strip() or DEFAULT_PERSONA
    except Exception:
        pass
    return DEFAULT_PERSONA


def save_persona(text: str) -> bool:
    """Save persona text to file. Returns True on success."""
    try:
        with _persona_lock:
            target = Path(PERSONA_FILE)
            target.parent.mkdir(parents=True, exist_ok=True)
            if not text.strip():
                target.unlink(missing_ok=True)
            else:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                                 suffix=".tmp", delete=False) as temporary:
                    temporary.write(text.strip())
                try:
                    os.replace(temporary.name, target)
                finally:
                    Path(temporary.name).unlink(missing_ok=True)
        return True
    except Exception:
        return False


class PersonaRequest(BaseModel):
    text: str


@app.get("/persona")
async def persona_get():
    text = get_persona()
    return {"text": text, "default": text == DEFAULT_PERSONA}


@app.post("/persona")
async def persona_set(req: PersonaRequest):
    ok = save_persona(req.text)
    if not ok:
        raise HTTPException(status_code=500, detail="人设保存失败，请检查数据目录权限。")
    return {"ok": ok, "message": "saved" if ok else "save failed"}


# ============================================================================
# Routes — terminal & sysinfo
# ============================================================================
@app.post("/terminal/exec")
async def terminal_exec(req: TerminalRequest):
    return run_safe_terminal_command(req.command)


@app.get("/sysinfo")
async def sysinfo():
    info: Dict[str, Any] = {
        "ok": True,
        "time": _now(),
        "python": sys.executable,
        "platform": sys.platform,
        "platform_name": platform.platform(),
        "mempalace_available": MEMPALACE_AVAILABLE,
        "memory_dir": MEMORY_DIR,
        "palace_path": MEMPALACE_PALACE_PATH,
        "wing": MEMPALACE_WING,
        "game": "None",
        "game_duration_min": 0,
        "gpu": "unknown",
    }
    if psutil is not None:
        try:
            info.update({
                "cpu_percent": psutil.cpu_percent(interval=0.1),
                "memory_percent": psutil.virtual_memory().percent,
                "mem": f"{psutil.virtual_memory().percent}%",
                "disk_percent": psutil.disk_usage(
                    str(PROJECT_DIR.anchor or PROJECT_DIR)
                ).percent,
            })
        except Exception as e:
            info["psutil_error"] = str(e)
    else:
        info["psutil_error"] = "psutil not installed"
    return info


# ============================================================================
# TTS config
# ============================================================================
FISH_SPEECH_API_KEY: str = os.getenv("FISH_SPEECH_API_KEY", "")
FISH_SPEECH_VOICE: str = os.getenv("FISH_SPEECH_VOICE", "nova")
FISH_SPEECH_URL: str = os.getenv("FISH_SPEECH_URL", "https://api.fish.audio/v1/tts")
FISH_SPEECH_AVAILABLE: bool = bool(FISH_SPEECH_API_KEY)

class SpeakRequest(BaseModel):
    text: str = ""


# ============================================================================
# Routes — TTS
# ============================================================================
@app.get("/speak")
@app.post("/speak")
async def speak(text: str = Query(default=""), body: Optional[SpeakRequest] = None):
    """Return an MP3 audio response via Fish Speech (primary) or edge-tts (fallback).

    Supports both GET (short texts) and POST (long texts, avoids 414 URI Too Long).
    """
    # POST body takes priority over query param
    if body and body.text:
        text = body.text
    text = (text or "").strip()
    if not text:
        return Response(status_code=204)
    if len(text) > 1500:
        raise HTTPException(status_code=400, detail="单次语音最多支持 1500 字。")

    # Prefer Fish Speech if API key is configured
    if FISH_SPEECH_AVAILABLE:
        try:
            r = await asyncio.get_running_loop().run_in_executor(executor, lambda: requests.post(
                FISH_SPEECH_URL,
                json={"text": text, "reference_id": FISH_SPEECH_VOICE},
                headers={
                    "Authorization": f"Bearer {FISH_SPEECH_API_KEY}",
                    "Content-Type": "application/json",
                },
                timeout=(10, 30),
            ))
            if r.status_code == 200:
                return Response(content=r.content, media_type="audio/mpeg")
            else:
                logger.warning(f"[FISH] HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            logger.warning(f"[FISH] failed: {e}")

    # Fallback to edge-tts
    if edge_tts is not None:
        voice = os.getenv("EDGE_TTS_VOICE", "zh-CN-XiaoxiaoNeural")
        try:
            async def audio_bytes():
                data = bytearray()
                communicate = edge_tts.Communicate(text=text, voice=voice)
                async for chunk in communicate.stream():
                    if chunk["type"] == "audio":
                        data.extend(chunk["data"])
                return bytes(data)
            data = await asyncio.wait_for(audio_bytes(), timeout=25)
            if data:
                return Response(content=data, media_type="audio/mpeg")
        except Exception as e:
            logger.warning(f"[TTS] edge_tts fallback failed: {e}")

    # Both providers failed — return 204 so frontend knows no audio available
    return Response(status_code=204)


# ============================================================================
# Routes — chat
# ============================================================================
@app.post("/chat")
async def chat(req: ChatRequest, request: Request, response: Response):
    user_input = (req.user_input or req.message or "").strip()
    if not user_input or len(user_input) > 8000:
        raise HTTPException(status_code=400, detail="请输入 1 至 8000 字的聊天内容。")
    if PUBLIC_MODE:
        state = _get_public_session(request, response)
        return await asyncio.get_running_loop().run_in_executor(
            executor, run_public_chat, user_input, req.mode, state,
        )
    result = await asyncio.get_running_loop().run_in_executor(executor, run_chat_logic, user_input, req.mode)

    try:
        await ws_manager.broadcast({
            "type": "chat",
            "user_input": user_input,
            "response": result.get("response"),
            "memory_debug": result.get("memory_debug"),
            "time": _now(),
        })
    except Exception:
        pass

    return result


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await ws_manager.connect(websocket)
    try:
        while True:
            data = await websocket.receive_text()
            if data.strip() == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            try:
                payload = json.loads(data)
            except Exception:
                payload = {"message": data}

            if not isinstance(payload, dict):
                await websocket.send_json({"type": "error", "message": "消息格式不正确。"})
                continue
            if payload.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
                continue
            if PUBLIC_MODE:
                await websocket.send_json({"type": "error", "message": "请通过网页聊天框发送消息。"})
                continue

            msg = payload.get("user_input") or payload.get("message") or ""
            mode = payload.get("mode", "fast")

            if not isinstance(msg, str) or not msg.strip() or len(msg) > 8000 or mode not in {"fast", "deep"}:
                await websocket.send_json({"type": "error", "message": "聊天内容或模式不正确。"})
                continue
            result = await asyncio.get_running_loop().run_in_executor(
                executor, run_chat_logic, msg.strip(), mode,
            )
            await websocket.send_json(result)
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        ws_manager.disconnect(websocket)
        logger.warning(f"WebSocket error: {e}")


@app.get("/chat")
async def chat_page():
    """Return the SPA index page, or a minimal HTML stub if missing."""
    index = PROJECT_DIR / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return HTMLResponse(
        """<html>
        <head><meta charset="utf-8"><title>LINGSHU API</title></head>
        <body>
        <h3>LINGSHU API is running.</h3>
        <p>POST /chat with JSON: {"user_input": "...", "mode": "fast"}</p>
        </body>
        </html>"""
    )


# ============================================================================
# Routes — robot (ElectronBot)
# ============================================================================

def _get_robot_or_error():
    """Return the robot instance or raise an HTTP error dict."""
    if not ROBOT_AVAILABLE or get_robot is None:
        return None, {
            "ok": False,
            "error": "Robot bridge not available. Install pyusb and install libusb driver.",
        }
    robot = get_robot()
    return robot, None


@app.get("/robot/status")
async def robot_status():
    """/robot/status — get connection status and joint readings"""
    robot, err = _get_robot_or_error()
    if err:
        return err

    return {
        "ok": True,
        "robot_available": ROBOT_AVAILABLE,
        "connected": robot.is_connected,
        "joints": robot.get_status().get("joints", [0] * 6),
    }


@app.post("/robot/connect")
async def robot_connect():
    """/robot/connect — connect to ElectronBot via USB"""
    robot, err = _get_robot_or_error()
    if err:
        return err

    if robot.is_connected:
        return {"ok": True, "message": "Already connected."}

    ok = robot.connect()
    if ok:
        robot.start_sync()
        return {"ok": True, "message": "Connected and sync started."}
    else:
        return {
            "ok": False,
            "message": (
                "Connection failed. Check: 1) ElectronBot is plugged in via USB, "
                "2) WinUSB/libusb driver is installed (use Zadig). "
                f"Looking for VID={robot_bridge.USB_VID:#06x}, PID={robot_bridge.USB_PID:#06x}."
            ),
        }


@app.post("/robot/disconnect")
async def robot_disconnect():
    """/robot/disconnect — disconnect from ElectronBot"""
    robot, err = _get_robot_or_error()
    if err:
        return err

    robot.disconnect()
    return {"ok": True, "message": "Disconnected."}


@app.post("/robot/joints")
async def robot_set_joints(req: RobotJointsRequest):
    """/robot/joints — set 6 servo angles (clamped in robot_bridge for hardware safety)"""
    robot, err = _get_robot_or_error()
    if err:
        return err

    if not robot.is_connected:
        return {"ok": False, "error": "Robot not connected. POST /robot/connect first."}

    ok, warnings = robot.set_joints(
        req.j1, req.j2, req.j3, req.j4, req.j5, req.j6, enable=req.enable
    )
    return {
        "ok": ok,
        "joints": [req.j1, req.j2, req.j3, req.j4, req.j5, req.j6],
        "enable": req.enable,
        "warnings": warnings or None,
    }


@app.get("/robot/joints")
async def robot_get_joints():
    """/robot/joints (GET) — read current joint angles from device"""
    robot, err = _get_robot_or_error()
    if err:
        return err

    if not robot.is_connected:
        return {"ok": False, "error": "Robot not connected."}

    joints = robot.get_joints()
    return {"ok": True, "joints": list(joints)}


@app.post("/robot/display")
async def robot_display(req: RobotDisplayRequest):
    """/robot/display — show an image or preset emoji on the robot screen"""
    robot, err = _get_robot_or_error()
    if err:
        return err

    if not robot.is_connected:
        return {"ok": False, "error": "Robot not connected. POST /robot/connect first."}

    import base64

    # --- emoji presets ---
    EMOJI_DIR = PROJECT_DIR / "assets" / "emoji"
    emoji_files = {
        "不屑": EMOJI_DIR / "不屑" / "resize_240.png",
        "兴奋": EMOJI_DIR / "兴奋" / "resize_240.png",
        "惊恐": EMOJI_DIR / "惊恐" / "resize_240.png",
        "愤怒": EMOJI_DIR / "愤怒" / "resize_240.png",
        "难过": EMOJI_DIR / "难过" / "resize_240.png",
        "静态": EMOJI_DIR / "静态" / "resize_240.png",
    }
    # English aliases
    emoji_aliases = {
        "happy": "兴奋", "excited": "兴奋",
        "sad": "难过",
        "angry": "愤怒",
        "surprised": "惊恐", "shocked": "惊恐",
        "neutral": "静态", "idle": "静态",
        "disdain": "不屑", "contempt": "不屑",
    }

    emoji_name = (req.emoji or "").strip()
    if emoji_name:
        # Resolve alias
        resolved = emoji_aliases.get(emoji_name.lower(), emoji_name)
        if resolved in emoji_files:
            filepath = emoji_files[resolved]
            if Path(filepath).exists():
                try:
                    from PIL import Image
                    img = Image.open(filepath).convert("RGB")
                    img = img.resize((240, 240), Image.LANCZOS)
                    raw = img.tobytes()
                    robot.set_image(raw, fmt="rgb")
                    return {"ok": True, "emoji": emoji_name, "resolved": resolved}
                except Exception as e:
                    return {"ok": False, "error": f"Emoji load failed: {e}"}
            else:
                return {"ok": False, "error": f"Emoji file not found: {resolved} → {filepath}"}
        else:
            return {"ok": False, "error": f"Unknown emoji: {emoji_name}. Known: {list(emoji_files.keys())}"}

    # --- base64 image ---
    image_b64 = (req.image or "").strip()
    if image_b64:
        try:
            # Strip data URI prefix if present
            if "," in image_b64:
                image_b64 = image_b64.split(",", 1)[1]
            img_bytes = base64.b64decode(image_b64)
            robot.set_image(img_bytes, fmt="jpeg")
            return {"ok": True, "size": len(img_bytes)}
        except Exception as e:
            return {"ok": False, "error": f"Image decode failed: {e}"}

    return {"ok": False, "error": "Provide either 'emoji' or 'image' field."}


@app.get("/robot/gesture")
async def robot_get_gesture():
    """/robot/gesture — read gesture sensor data from extraData"""
    robot, err = _get_robot_or_error()
    if err:
        return err

    if not robot.is_connected:
        return {"ok": False, "error": "Robot not connected."}

    extra = robot.get_extra_data()
    return {"ok": True, "extra_data_hex": extra.hex(), "extra_data_bytes": list(extra)}


@app.post("/robot/raw_extra")
async def robot_set_extra(req: dict):
    """/robot/raw_extra — send raw extraData (advanced)"""
    robot, err = _get_robot_or_error()
    if err:
        return err

    if not robot.is_connected:
        return {"ok": False, "error": "Robot not connected."}

    data_bytes = req.get("data", [])
    if isinstance(data_bytes, str):
        import base64
        data_bytes = base64.b64decode(data_bytes)
    elif isinstance(data_bytes, list):
        data_bytes = bytes(data_bytes)

    robot.set_extra_data(data_bytes)
    return {"ok": True, "size": len(data_bytes)}


@app.get("/robot/self_test")
async def robot_self_test():
    """/robot/self_test — check if ElectronBot hardware is reachable"""
    robot, err = _get_robot_or_error()
    if err:
        return err

    result = robot.self_test()
    return result


# ============================================================================
# Standalone entry point
# ============================================================================
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api_server:app", host=HOST, port=PORT, reload=False)
