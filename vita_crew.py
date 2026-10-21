# -*- coding: utf-8 -*-
"""
Vita Saria AI engine — DeepSeek integration, tool execution, and prompt construction.

Handles:
- Fast chat via deepseek-chat
- Deep reasoning via deepseek-reasoner
- Tool tag parsing, risk grading, and safe execution
- System prompt assembly with role-play context
"""
from __future__ import annotations

import datetime
import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL: str = os.getenv("DEEPSEEK_URL", "https://api.deepseek.com/chat/completions")

ALLOW_MODEL_TOOLS: bool = os.getenv("LINGSHU_ALLOW_MODEL_TOOLS", "1") == "1"
ALLOW_RISKY_TOOLS: bool = os.getenv("LINGSHU_ALLOW_RISKY_TOOLS", "0") == "1"

session = requests.Session()
logger = logging.getLogger("lingshu.vita_crew")

# ---------------------------------------------------------------------------
# Import tool registry from skills module
# ---------------------------------------------------------------------------
try:
    from skills import TOOL_REGISTRY, execute_tool, get_tools_description
except ImportError:
    logging.warning("skills module not found — tools will be unavailable")
    TOOL_REGISTRY: Dict[str, Callable[..., str]] = {}
    execute_tool = lambda *a, **k: "Tools unavailable"
    get_tools_description = lambda: ""


# ---------------------------------------------------------------------------
# Tool risk grading
# ---------------------------------------------------------------------------
RISKY_TOOL_NAME_PATTERNS: list[str] = [
    r"exec", r"terminal", r"shell", r"cmd", r"powershell",
    r"delete", r"remove", r"rm", r"move", r"rename",
    r"write", r"create", r"edit", r"modify", r"save",
    r"clipboard", r"system", r"process", r"kill",
]

SAFE_TOOL_ALLOWLIST: set[str] = {
    "get_time",
    "web_search",
    "search_web",
    "weather",
    "calculator",
    "sysinfo",
    # Robot (ElectronBot) tools
    "robot_connect",
    "robot_disconnect",
    "robot_status",
    "robot_self_test",
    "robot_display_emoji",
    "robot_set_joints",
}


# ---------------------------------------------------------------------------
# Complexity heuristics
# ---------------------------------------------------------------------------
COMPLEX_KEYWORDS: list[str] = [
    "code", "explain", "why", "derive", "write", "analyze",
    "error", "bug", "logic", "plan", "principle", "optimize",
]

def is_complex_task(text: str) -> bool:
    """Return True if the input suggests a task that warrants deep reasoning."""
    has_keyword = any(kw in text for kw in COMPLEX_KEYWORDS)
    is_long = len(text) > 200
    # Require both a keyword AND length, OR very long input (>300 chars)
    return (has_keyword and len(text) > 60) or len(text) > 300


# ---------------------------------------------------------------------------
# DeepSeek Reasoner (deep mode)
# ---------------------------------------------------------------------------
def call_deepseek_reasoner(user_input: str) -> Tuple[Optional[str], Optional[str]]:
    """Call deepseek-reasoner and return (reasoning_summary, content)."""
    if not DEEPSEEK_API_KEY:
        return None, None

    for attempt in range(2):
        try:
            r = session.post(
                DEEPSEEK_URL,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                },
                json={
                    "model": "deepseek-reasoner",
                    "messages": [{"role": "user", "content": user_input}],
                },
                timeout=(10, 90),
            )
            r.raise_for_status()
            d = r.json()
            m = d["choices"][0]["message"]
            reasoning_content = m.get("reasoning_content", "") or ""
            content = m.get("content", "") or ""

            reasoning_summary = "Deep analysis complete." if reasoning_content else None
            return reasoning_summary, content
        except Exception as e:
            logger.error(f"DeepSeek reasoner attempt {attempt + 1} failed: {e}")
            if attempt == 0:
                continue
    return None, None


# ---------------------------------------------------------------------------
# Tool execution safety
# ---------------------------------------------------------------------------
def is_risky_tool(tool_name: str) -> bool:
    """Check whether a tool name matches risky patterns."""
    lower_name = tool_name.lower()
    if lower_name in SAFE_TOOL_ALLOWLIST:
        return False
    return any(re.search(pattern, lower_name) for pattern in RISKY_TOOL_NAME_PATTERNS)


def parse_tool_params(raw_params: str) -> dict:
    """Parse tool parameters from `<tool>name|key=val|key=val</tool>` syntax."""
    kwargs: Dict[str, str] = {}
    for part in (raw_params or "").split("|"):
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k:
                kwargs[k] = v
    return kwargs


def execute_tool_safely(tool_name: str, kwargs: dict) -> str:
    """Execute a tool with safety checks. Returns a system message string."""
    if not ALLOW_MODEL_TOOLS:
        return "[SYSTEM: Model tool calling is disabled.]"

    if os.getenv("LINGSHU_PUBLIC_MODE", "0") == "1" and tool_name not in {
        "get_time", "calculator", "web_search", "search_web", "weather",
    }:
        return "[SYSTEM: This tool is unavailable in the public web demo.]"

    if tool_name not in TOOL_REGISTRY:
        return f"[SYSTEM: Tool not found: {tool_name}]"

    if is_risky_tool(tool_name) and not ALLOW_RISKY_TOOLS:
        return (
            f"[SYSTEM: Tool '{tool_name}' is high-risk and was blocked. "
            "Set LINGSHU_ALLOW_RISKY_TOOLS=1 to enable.]"
        )

    try:
        result = execute_tool(tool_name, **kwargs)
        return f"[SYSTEM: {result}]"
    except Exception as e:
        logger.error(f"Tool execution failed: {tool_name}: {e}")
        return f"[SYSTEM: Tool execution error: {e}]"


def extract_and_execute_tools(text: str, max_iterations: int = 2) -> str:
    """Find and execute all `<tool>...</tool>` tags in text, then inline results."""
    pattern = r"<tool>(\w+)(?:\|(.+?))?</tool>"
    result_text = text

    for _ in range(max_iterations):
        matches = list(re.finditer(pattern, result_text))
        if not matches:
            break

        parts: list[str] = []
        last_end = 0
        for m in matches:
            before = result_text[last_end:m.start()].strip()
            if before:
                parts.append(before)

            tool_name = m.group(1).strip()
            kwargs = parse_tool_params(m.group(2) or "")
            parts.append(execute_tool_safely(tool_name, kwargs))
            last_end = m.end()

        remaining = result_text[last_end:].strip()
        if remaining:
            parts.append(remaining)
        result_text = "\n".join(parts)

    return result_text


# ---------------------------------------------------------------------------
# System prompt assembly
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Scenario classification — lightweight keyword-based, runs before every LLM call
# ---------------------------------------------------------------------------
# Priority: driving > emotional > off_duty_health > social_risk

DRIVING_KEYWORDS: list[str] = [
    # Active operation terms
    "开车", "跑车", "出车", "收车", "线路", "故障", "报警", "信号",
    "瞭望", "制动", "牵引", "列尾", "编组", "调度", "进路",
    "发车", "到站", "通过", "限速", "施工", "天窗", "闭塞",
    "机车", "车辆", "弓网", "接触网", "道岔", "进站", "出站",
    "行车", "运行", "操纵", "待乘", "出乘", "退乘", "紧急",
    "异响", "异常", "振动", "报警灯", "指示灯", "显示屏",
]

EMOTIONAL_KEYWORDS: list[str] = [
    # Mood / emotional distress
    "难过", "伤心", "哭了", "呜呜", "郁闷", "委屈", "低落", "焦虑",
    "压力", "想哭", "烦躁", "崩溃", "心里堵", "不好受", "撑不住",
    "憋屈", "心累", "心塞", "叹气", "没意思", "想不开",
    "不想说话", "提不起", "没劲", "丧", "emo",
    # Somatic-emotional blur
    "闷", "烦", "孤独", "一个人",
]

OFF_DUTY_HEALTH_KEYWORDS: list[str] = [
    # Physical fatigue & body
    "累", "困", "酸", "疼", "难受", "疲劳", "失眠", "睡不好",
    "腰", "背", "肩", "脖子", "眼睛", "手腕", "腿酸", "脚",
    "没睡好", "睡不着", "醒了", "熬夜",
    # Health & recovery
    "气血", "泡脚", "按摩", "锻炼", "跑步", "运动",
    "身体", "状态", "体检", "健康", "饮食", "吃饭", "胃口",
    # Off-duty signals
    "下班", "到家", "回家", "休息", "放假", "调休",
    "我回来了", "回来了", "早安", "晚安", "晚上好",
]

SOCIAL_RISK_KEYWORDS: list[str] = [
    # Dependency signals
    "想你", "离不开你", "多陪", "陪我聊", "别走",
    "你最好", "只有你", "没人懂", "不想跟人说",
    # Isolation signals
    "不想出门", "懒得出门", "没人聊天", "除了你没别人",
    "天天一个人", "好多天没", "好久没联系",
]


def _detect_scenario(user_input: str) -> str:
    """Classify current conversation scenario via keyword matching.

    Returns: 'driving' | 'emotional' | 'off_duty_health' | 'social_risk' | 'default'
    """
    text = (user_input or "").lower()

    if any(kw in text for kw in DRIVING_KEYWORDS):
        return "driving"
    if any(kw in text for kw in EMOTIONAL_KEYWORDS):
        return "emotional"
    if any(kw in text for kw in OFF_DUTY_HEALTH_KEYWORDS):
        return "off_duty_health"
    if any(kw in text for kw in SOCIAL_RISK_KEYWORDS):
        return "social_risk"
    return "default"


# ---------------------------------------------------------------------------
# Prompt layers — core soul (≤300字) + scene micro-instructions (~150字 each)
# ---------------------------------------------------------------------------

def _build_core_prompt(t: str, w: str, desktop: str, downloads: str) -> str:
    """Layer 1: 核心人设. Reads persona file if exists, else uses default (from api_server)."""
    try:
        from api_server import get_persona
        persona = get_persona()
    except Exception:
        persona = "你是路遥，一个话不多、有分寸的中年铁路人。"
    context = f"当前时间:{t} ({w})"
    if os.getenv("LINGSHU_PUBLIC_MODE", "0") != "1":
        context += f"  桌面:{desktop}  下载:{downloads}"
    return f"{persona}\n{context}"


# Scene micro-instructions — ~80字 each, injected on demand
SCENE_INSTRUCTIONS: dict[str, str] = {
    "driving": (
        "【行车中】别闲聊。只在安全或故障时用最短的话提醒。"
    ),
    "emotional": (
        "【他心情不好】先接住情绪让他觉得你懂他，再帮他把事情理清楚，"
        "最后给一个今晚就能做的小建议。别讲道理，别评价他。"
    ),
    "off_duty_health": (
        "【退勤了】聊聊身体状态，用气血解释疲劳，给饮食睡眠运动建议。"
        "像哥们儿聊天，该夸就夸。"
    ),
    "social_risk": (
        "【注意分寸】对方有点依赖你了。话题该收了，往他家人同事那边引。"
    ),
}


def _build_technical_appendix(tools_desc: str) -> str:
    """Layer 3: Technical — tools & format. Always appended after soul + scene."""
    return f"""[Tools]
{tools_desc}

Tool format: <tool>name|k=v</tool>
安全：不调用 delete/write/move/exec 工具。
记住：你是在聊天，不是在写小说。不要有任何动作描写。"""


def _build_system_prompt(user_input: str = "") -> str:
    """Assemble the full layered system prompt.

    Layer 1: core soul (≤300字) — always sent.
    Layer 2: scene micro-instruction (~150字) — injected on demand.
    Layer 3: technical appendix (tools + format) — always appended.
    """
    now = datetime.datetime.now()
    weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    w = weekday_names[now.weekday()]
    t = now.strftime("%Y-%m-%d %H:%M:%S")

    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    downloads = os.path.join(os.path.expanduser("~"), "Downloads")
    tools_desc = get_tools_description()
    if os.getenv("LINGSHU_PUBLIC_MODE", "0") == "1":
        tools_desc = "\n".join(line for line in tools_desc.splitlines()
                               if line.split("：", 1)[0] in {
                                   "get_time", "calculator", "web_search", "weather",
                               })

    scenario = _detect_scenario(user_input)

    parts = [_build_core_prompt(t, w, desktop, downloads)]

    instruction = SCENE_INSTRUCTIONS.get(scenario)
    if instruction:
        parts.append(instruction)
    # "default" → no scene instruction injected

    parts.append(_build_technical_appendix(tools_desc))

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# DeepSeek Chat (fast mode)
# ---------------------------------------------------------------------------
def fast_deepseek_response(
    user_input: str,
    enhanced_input: Optional[str] = None,
    deep_report: Optional[str] = None,
    messages: Optional[List[Dict[str, str]]] = None,
    memory_context: str = "",
) -> str:
    """Call deepseek-chat with the Vita role-play system prompt.

    Parameters
    ----------
    user_input : str
        The current user message.
    messages : list[dict], optional
        Structured conversation history as [{"role": "user/assistant", "content": "..."}].
        When provided, these are inserted between the system prompt and the current user message
        so the model sees a proper multi-turn conversation.
    memory_context : str
        Long-term memory search results to inject into the current user message.
    """
    if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "sk-your-api-key-here":
        return "尚未配置聊天服务。请在服务器 .env 中填写 DeepSeek API Key 后重启；现在可以点击 DEMO 查看预设演示。"

    system_prompt = _build_system_prompt(user_input)

    # Build current user content: memory + deep analysis + user input
    user_content_parts: list[str] = []
    if memory_context:
        user_content_parts.append(f"[Long-term Memory]\n{memory_context}")
    if deep_report:
        user_content_parts.append(f"[Analysis]\n{deep_report}")
    user_content_parts.append(f"[User Input]\n{user_input}")
    user_content = "\n\n".join(user_content_parts)

    # Assemble the messages array: system + history + current user
    api_messages: list[dict] = [{"role": "system", "content": system_prompt}]
    if messages:
        api_messages.extend(messages)
    api_messages.append({"role": "user", "content": user_content})

    def _post_chat(msgs: list[dict], max_tokens: int = 1024) -> str:
        for attempt in range(3):
            try:
                r = session.post(
                    DEEPSEEK_URL,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                    },
                    json={
                        "model": "deepseek-chat",
                        "messages": msgs,
                        "temperature": 0.5,
                        "max_tokens": max_tokens,
                        "stream": False,
                    },
                    timeout=(10, 90),
                )
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"].strip()
            except Exception as e:
                logger.warning(f"DeepSeek chat attempt {attempt + 1}/3 failed: {e}")
                if attempt == 2:
                    raise
                time.sleep(1.0 * (attempt + 1))  # linear backoff

    try:
        raw = _post_chat(api_messages, max_tokens=1024)

        # If the model emits tool tags, execute them and make a follow-up call
        if "<tool>" not in raw:
            return raw

        tool_executed = extract_and_execute_tools(raw)
        api_messages.append({"role": "assistant", "content": raw})
        api_messages.append({
            "role": "user",
            "content": (
                f"System feedback:\n{tool_executed}\n\n"
                f"Reply concisely using the feedback above. Do not expose tool tags."
            ),
        })
        return _post_chat(api_messages, max_tokens=512)

    except Exception as e:
        logger.error(f"DeepSeek chat failure after retries: {e}")
        return "聊天服务暂时没有响应，请检查网络和 API 配置后重试。也可以点击 DEMO 查看预设演示。"


# Backward-compatible alias
fast_ollama_response = fast_deepseek_response


# ---------------------------------------------------------------------------
# Crew output wrapper
# ---------------------------------------------------------------------------
class CrewResult:
    """Result object mimicking the crew pattern."""
    def __init__(self, text: str, thinking_log: Optional[str] = None):
        self.raw = text
        self.thinking_log = thinking_log

    def kickoff(self):
        return self


# FakeCrewResult kept for backward compatibility
FakeCrewResult = CrewResult


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------
def create_vita_crew(
    original_user_input: str,
    enhanced_input: Optional[str] = None,
    force_deep: bool = False,
    messages: Optional[List[Dict[str, str]]] = None,
    memory_context: str = "",
) -> CrewResult:
    """Orchestrate the full Vita response pipeline.

    Parameters
    ----------
    original_user_input : str
        Raw user query.
    enhanced_input : str, optional
        Memory-augmented version of the input (legacy, prefer messages + memory_context).
    force_deep : bool
        If True, always use deep reasoning (deepseek-reasoner).
    messages : list[dict], optional
        Structured conversation history for multi-turn context.
    memory_context : str
        Long-term memory search results.

    Returns
    -------
    CrewResult
        Object with `.raw` (final response) and `.thinking_log` (reasoning summary).
    """
    thinking_log: Optional[str] = None
    deep_report: Optional[str] = None

    if force_deep or is_complex_task(original_user_input):
        thinking_log, deep_report = call_deepseek_reasoner(original_user_input)
        if not deep_report:
            deep_report = None

    text = fast_deepseek_response(
        original_user_input,
        enhanced_input=enhanced_input,
        deep_report=deep_report,
        messages=messages,
        memory_context=memory_context,
    )
    return CrewResult(text, thinking_log)
