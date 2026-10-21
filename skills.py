# -*- coding: utf-8 -*-
"""
Safe built-in tools for LINGSHU.

Provides TOOL_REGISTRY, execute_tool, and get_tools_description
for vita_crew.py without depending on an external tools package.

Only low-risk read-only tools are included by default.
"""
from __future__ import annotations

import datetime
import ast
import math
import operator
import os
import platform
import socket
from pathlib import Path
from typing import Callable, Dict, Any

try:
    import requests
except Exception:
    requests = None

try:
    import psutil
except Exception:
    psutil = None


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def get_time() -> str:
    """Return current local datetime including Chinese weekday."""
    now = datetime.datetime.now()
    weekday_names = [
        "\u661f\u671f\u4e00", "\u661f\u671f\u4e8c", "\u661f\u671f\u4e09",
        "\u661f\u671f\u56db", "\u661f\u671f\u4e94", "\u661f\u671f\u516d", "\u661f\u671f\u65e5",
    ]
    weekday = weekday_names[now.weekday()]
    return now.strftime(f"%Y-%m-%d %H:%M:%S\uff08{weekday}\uff09")


def calculator(expression: str = "") -> str:
    """Evaluate numeric expressions without Python attribute access or eval."""
    expression = (expression or "").strip()
    if not expression:
        return "\u6ca1\u6709\u63d0\u4f9b\u8868\u8fbe\u5f0f\u3002"

    allowed: Dict[str, Any] = {
        name: getattr(math, name)
        for name in dir(math)
        if not name.startswith("_")
    }
    allowed.update({
        "abs": abs, "round": round, "min": min,
        "max": max, "sum": sum, "pow": pow,
    })
    binary = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
              ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
              ast.Pow: operator.pow}

    def checked(value):
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e100:
            raise ValueError("数值超出计算范围")
        return value

    def evaluate(node):
        if isinstance(node, ast.Constant):
            return checked(node.value)
        if isinstance(node, ast.Name) and node.id in allowed and not callable(allowed[node.id]):
            return checked(allowed[node.id])
        if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) <= 32:
            return [evaluate(item) for item in node.elts]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = checked(evaluate(node.operand))
            return checked(-value if isinstance(node.op, ast.USub) else value)
        if isinstance(node, ast.BinOp) and type(node.op) in binary:
            left, right = checked(evaluate(node.left)), checked(evaluate(node.right))
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("指数过大")
            return checked(binary[type(node.op)](left, right))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            name = node.func.id
            function = allowed.get(name)
            if not callable(function) or len(node.args) > 32:
                raise ValueError("不支持此函数")
            args = [evaluate(arg) for arg in node.args]
            if name in {"factorial", "comb", "perm"} and any(abs(checked(arg)) > 500 for arg in args):
                raise ValueError("参数过大")
            if name == "pow" and (len(args) > 2 or len(args) == 2 and abs(checked(args[1])) > 100):
                raise ValueError("指数过大")
            return checked(function(*args))
        raise ValueError("只支持数字、四则运算及数学函数")

    try:
        if len(expression) > 512:
            raise ValueError("表达式过长")
        tree = ast.parse(expression, mode="eval")
        if len(list(ast.walk(tree))) > 128:
            raise ValueError("表达式过于复杂")
        result = checked(evaluate(tree.body))
        return str(result)
    except Exception as exc:
        return f"\u8ba1\u7b97\u5931\u8d25\uff1a{exc}"


def sysinfo() -> str:
    """Return basic system information."""
    parts = [
        f"\u7cfb\u7edf\uff1a{platform.system()} {platform.release()}",
        f"Python\uff1a{platform.python_version()}",
        f"\u4e3b\u673a\u540d\uff1a{socket.gethostname()}",
        f"\u5f53\u524d\u76ee\u5f55\uff1a{Path.cwd()}",
    ]
    if psutil is not None:
        try:
            parts.append(f"CPU\uff1a{psutil.cpu_percent(interval=0.1)}%")
            parts.append(f"\u5185\u5b58\uff1a{psutil.virtual_memory().percent}%")
        except Exception as exc:
            parts.append(f"psutil \u8bfb\u53d6\u5931\u8d25\uff1a{exc}")
    return "\n".join(parts)


def list_allowed_paths() -> str:
    """List commonly used file-system paths."""
    home = Path.home()
    return "\n".join([
        f"\u684c\u9762\uff1a{home / 'Desktop'}",
        f"\u4e0b\u8f7d\uff1a{home / 'Downloads'}",
        f"\u7528\u6237\u76ee\u5f55\uff1a{home}",
    ])


# ---------------------------------------------------------------------------
# Web / information tools
# ---------------------------------------------------------------------------

def web_search(query: str = "") -> str:
    """Search the web using DuckDuckGo Instant Answer API (free, no key needed)."""
    if requests is None:
        return "Network unavailable (requests module not installed)."

    query = (query or "").strip()
    if not query:
        return "No search query provided. Usage: <tool>web_search|query=keyword</tool>"

    try:
        url = "https://api.duckduckgo.com/"
        params = {
            "q": query,
            "format": "json",
            "no_html": "1",
            "skip_disambig": "1",
        }
        r = requests.get(url, params=params, timeout=12, headers={
            "User-Agent": "Mozilla/5.0 (compatible; LINGSHU/1.0)",
        })
        r.raise_for_status()
        data = r.json()

        parts: list[str] = []

        # Instant answer or abstract
        abstract = (data.get("AbstractText") or "").strip()
        if abstract:
            parts.append(abstract)
        elif data.get("Answer"):
            parts.append(f"Answer: {data['Answer']}")
        elif data.get("Abstract"):
            parts.append(data["Abstract"])

        # Fall back to related topics
        related = data.get("RelatedTopics", [])
        if not parts and related:
            for topic in related[:5]:
                if isinstance(topic, dict) and topic.get("Text"):
                    parts.append(f"- {topic['Text']}")

        # If still nothing, mention source
        if not parts:
            heading = (data.get("Heading") or "").strip()
            if heading:
                parts.append(f"No direct answer for \"{heading}\". Try a more specific query.")
            else:
                parts.append(f"No results found for: {query}")

        return "\n".join(parts)

    except Exception as e:
        return f"Search failed: {e}"


def weather(city: str = "") -> str:
    """Get current weather for a city using wttr.in (free, no API key)."""
    if requests is None:
        return "Network unavailable (requests module not installed)."

    city = (city or "").strip()
    if not city:
        return "No city specified. Usage: <tool>weather|city=Beijing</tool>"

    try:
        # wttr.in provides plain-text weather; ?format options:
        # %C=condition, %t=temperature, %w=wind, %h=humidity
        url = f"https://wttr.in/{requests.utils.quote(city)}"
        params = {
            "format": "%C  %t  Wind: %w  Humidity: %h",
        }
        r = requests.get(url, params=params, timeout=10, headers={
            "User-Agent": "curl/8.0",  # wttr.in requires curl-like UA for plain text
        })
        r.raise_for_status()
        text = r.text.strip()
        if not text or len(text) > 300:
            # Fallback: get the full ANSI report and strip escape codes
            r2 = requests.get(
                f"https://wttr.in/{requests.utils.quote(city)}?0T",
                timeout=10,
                headers={"User-Agent": "curl/8.0"},
            )
            text = r2.text.strip()
            # Strip ANSI escape sequences
            import re
            text = re.sub(r'\x1b\[[0-9;]*m', '', text)
            # Take first few meaningful lines
            lines = [l for l in text.split("\n") if l.strip()][:8]
            text = "\n".join(lines)

        return f"Weather for {city}:\n{text}"

    except Exception as e:
        return f"Weather query failed: {e}"


# ---------------------------------------------------------------------------
# Robot (ElectronBot) tools — safe HW interaction
# ---------------------------------------------------------------------------

def _get_robot():
    """Lazy-load the robot bridge singleton."""
    try:
        from robot_bridge import get_robot
        return get_robot()
    except Exception:
        return None


def robot_status() -> str:
    """Get ElectronBot connection status and joint readings."""
    robot = _get_robot()
    if robot is None:
        return "Robot bridge not available (pyusb missing or driver not installed)."
    status = robot.get_status()
    return (
        f"Connected: {status.get('connected', False)}. "
        f"Joints: {status.get('joints', [])}"
    )


def robot_display_emoji(emoji: str = "happy") -> str:
    """Display a preset emoji on the ElectronBot screen.
    Options: happy, sad, angry, surprised, neutral, excited, disdain."""
    robot = _get_robot()
    if robot is None:
        return "Robot bridge not available."
    if not robot.is_connected:
        return "Robot not connected. Use robot_connect first."

    from pathlib import Path

    EMOJI_DIR = Path(__file__).resolve().parent / "assets" / "emoji"

    # Preferred MP4-extracted frames (from face/*.mp4) — try these first,
    # fall back to resize_240.png (PIL-generated) if missing.
    _emoji_preferred: dict[str, list[str]] = {
        "兴奋": ["excited.png", "hello.png", "resize_240.png"],
        "难过": ["sad.png", "goodbye.png", "resize_240.png"],
        "惊恐": ["fear.png", "resize_240.png"],
        "愤怒": ["anger.png", "resize_240.png"],
        "不屑": ["disdain.png", "resize_240.png"],
        "静态": ["normal.png", "resize_240.png"],
    }

    def _find_emoji_file(cn_name: str) -> Path:
        """Find the best available emoji file for a Chinese emoji name."""
        candidates = _emoji_preferred.get(cn_name, ["resize_240.png"])
        emoji_folder = EMOJI_DIR / cn_name
        for fname in candidates:
            p = emoji_folder / fname
            if p.exists():
                return p
        return emoji_folder / candidates[-1]  # return last as fallback

    emoji_aliases = {
        "happy": "兴奋", "excited": "兴奋",
        "sad": "难过",
        "angry": "愤怒",
        "surprised": "惊恐", "shocked": "惊恐",
        "neutral": "静态", "idle": "静态",
        "disdain": "不屑", "contempt": "不屑",
        "hello": "兴奋", "bye": "难过",
    }

    emoji_name = emoji.strip()
    # If emoji_name is an English alias, resolve it; otherwise treat as raw Chinese name
    resolved = emoji_aliases.get(emoji_name.lower(), emoji_name)

    if resolved not in _emoji_preferred:
        known = ", ".join(
            f"{cn}({', '.join(k for k,v in emoji_aliases.items() if v==cn)})"
            for cn in _emoji_preferred
        )
        return f"Unknown emoji: {emoji_name}. Available: {known}"

    filepath = _find_emoji_file(resolved)
    if not filepath.exists():
        return f"Emoji file not found: {resolved} -> {filepath}"

    try:
        from PIL import Image

        img = Image.open(filepath).convert("RGB")
        img = img.resize((240, 240), Image.LANCZOS)
        # ElectronBot firmware expects BGR888 byte order
        r, g, b = img.split()
        img = Image.merge("RGB", (b, g, r))
        robot.set_image(img.tobytes(), fmt="rgb")
        return f"Display emoji: {emoji_name} -> {resolved}"
    except Exception as e:
        return f"Display emoji failed: {e}"


def robot_set_joints(
    j1: str = "0", j2: str = "0", j3: str = "0",
    j4: str = "0", j5: str = "0", j6: str = "0",
) -> str:
    """Set all 6 servo angles on ElectronBot (degrees, float)."""
    robot = _get_robot()
    if robot is None:
        return "Robot bridge not available."
    if not robot.is_connected:
        return "Robot not connected. Use robot_connect first."
    
    try:
        robot.set_joints(
            float(j1), float(j2), float(j3),
            float(j4), float(j5), float(j6),
            enable=True,
        )
        joints = [j1, j2, j3, j4, j5, j6]
        return f"Servos set to {joints}."
    except Exception as e:
        return f"Set joints failed: {e}"


# ---------------------------------------------------------------------------
# Robo action tools — preset poses + combined emoji+pose
# ---------------------------------------------------------------------------

def robo_play_pose(pose: str = "") -> str:
    """Play a preset robot pose animation. Available poses: wave, anger, disdain,
    think, nod, shake, celebrate, look_around."""
    pose = (pose or "").strip().lower()
    if not pose:
        available = ", ".join(sorted(PRESET_POSES.keys()))
        return f"No pose specified. Available: {available}"

    try:
        from robo_actions import play_pose
        ok = play_pose(pose)
        return f"Pose '{pose}' played." if ok else f"Pose '{pose}' failed — robot connected?"
    except Exception as e:
        return f"Pose failed: {e}"


def robo_emotion(emoji: str = "happy", pose: str = "") -> str:
    """Show an emoji on screen AND play a pose simultaneously.
    Example: <tool>robo_emotion|emoji=excited|pose=celebrate</tool>"""
    emoji = (emoji or "happy").strip().lower()
    pose_name = (pose or "").strip().lower()

    parts = []
    # Show emoji
    result = robot_display_emoji(emoji=emoji)
    parts.append(result)

    # Play pose if specified
    if pose_name:
        result2 = robo_play_pose(pose=pose_name)
        parts.append(result2)

    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Preset poses — shared with robo_actions. Defined here to break circular import.
# Each pose is a list of (j1..j6, hold_seconds) tuples.
# After playback the robot returns to neutral [0,0,0,0,0,0].
# ---------------------------------------------------------------------------
PRESET_POSES: Dict[str, list] = {
    "wave": [
        (8, 0, 0, 0, 0, 43, 0.12),
        (15, 0, 0, 0, 0, 43, 0.12),
        (8, 0, 0, 0, 0, 43, 0.12),
        (15, 0, 0, 0, 0, 43, 0.12),
        (0, 0, 0, 0, 0, -1, 0.10),
        (14, 0, 0, 0, 0, -87, 0.15),
        (7, 0, 0, 0, 0, -38, 0.12),
        (14, 0, 0, 0, 0, -87, 0.12),
        (7, 0, 0, 0, 0, -38, 0.12),
        (0, 0, 0, 0, 0, 0, 0.20),
    ],
    "anger": [
        (6, 0, 43, 0, 40, 0, 0.08),
        (6, 0, 74, 0, 78, 0, 0.08),
        (11, 0, 105, 0, 111, 0, 0.08),
        (15, 0, 133, 0, 138, 0, 0.10),
        (15, 0, 180, 0, 180, 0, 0.15),
        (5, 0, 128, 0, 128, 0, 0.10),
        (0, 0, 86, 0, 89, 0, 0.10),
        (0, 0, 0, 0, 0, 0, 0.30),
    ],
    "disdain": [
        (8, 0, 0, 15, 27, 0, 0.10),
        (13, 0, 0, 30, 110, -87, 0.15),
        (13, 0, 0, 30, 86, -87, 0.15),
        (13, 0, 0, 12, 86, -87, 0.15),
        (13, 0, 0, 0, 0, 0, 0.15),
        (13, 0, 0, 0, 41, -87, 0.15),
        (0, 0, 0, 0, 0, 0, 0.30),
    ],
    "think": [
        (0, 0, 0, 0, 0, 47, 0.30),
        (0, 0, 0, 0, 0, 90, 0.50),
        (0, 0, 0, 0, 0, 47, 0.30),
        (0, 0, 0, 0, 0, 0, 0.20),
    ],
    "nod": [
        (8, 0, 0, 0, 0, 0, 0.20),
        (0, 0, 0, 0, 0, 0, 0.15),
        (8, 0, 0, 0, 0, 0, 0.20),
        (0, 0, 0, 0, 0, 0, 0.30),
    ],
    "shake": [
        (0, 0, 0, 0, 0, 47, 0.20),
        (0, 0, 0, 0, 0, -47, 0.20),
        (0, 0, 0, 0, 0, 47, 0.20),
        (0, 0, 0, 0, 0, -47, 0.20),
        (0, 0, 0, 0, 0, 0, 0.20),
    ],
    "celebrate": [
        (10, 0, 100, 0, 100, 0, 0.15),
        (15, 0, 150, 0, 150, 0, 0.20),
        (10, 0, 100, 0, 100, 0, 0.15),
        (15, 0, 150, 0, 150, 0, 0.20),
        (0, 0, 0, 0, 0, 0, 0.30),
    ],
    "look_around": [
        (0, 0, 0, 0, 0, 60, 0.25),
        (0, 0, 0, 0, 0, -60, 0.25),
        (0, 0, 0, 0, 0, 30, 0.20),
        (0, 0, 0, 0, 0, -30, 0.20),
        (0, 0, 0, 0, 0, 0, 0.20),
    ],
}


def robot_connect() -> str:
    """Connect to ElectronBot via USB and start sync."""
    robot = _get_robot()
    if robot is None:
        return "Robot bridge not available."
    if robot.is_connected:
        return "Already connected."
    
    ok = robot.connect()
    if ok:
        robot.start_sync()
        return "Connected and sync started."
    return "Connection failed. Check USB and driver (Zadig)."


def robot_disconnect() -> str:
    """Disconnect from ElectronBot."""
    robot = _get_robot()
    if robot is None:
        return "Robot bridge not available."
    robot.disconnect()
    return "Disconnected."


def robot_self_test() -> str:
    """Check if ElectronBot hardware is reachable via USB."""
    robot = _get_robot()
    if robot is None:
        return "Robot bridge not available."
    result = robot.self_test()
    return (
        f"Found: {result.get('found', False)}. "
        f"Connected: {result.get('connected', False)}. "
        f"{result.get('message', '')}"
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

TOOL_REGISTRY: Dict[str, Callable[..., str]] = {
    "get_time": get_time,
    "calculator": calculator,
    "sysinfo": sysinfo,
    "list_allowed_paths": list_allowed_paths,
    "web_search": web_search,
    "search_web": web_search,  # alias
    "weather": weather,
    # Robot tools
    "robot_status": robot_status,
    "robot_display_emoji": robot_display_emoji,
    "robot_set_joints": robot_set_joints,
    "robot_connect": robot_connect,
    "robot_disconnect": robot_disconnect,
    "robot_self_test": robot_self_test,
    # Robo action tools
    "robo_play_pose": robo_play_pose,
    "robo_emotion": robo_emotion,
}


def execute_tool(tool_name: str, **kwargs) -> str:
    """Execute a registered tool by name."""
    tool = TOOL_REGISTRY.get(tool_name)
    if tool is None:
        return f"\u5de5\u5177\u4e0d\u5b58\u5728\uff1a{tool_name}"
    return tool(**kwargs)


def get_tools_description() -> str:
    """Return a formatted description of all available tools."""
    lines = [
        "get_time：获取当前本地时间。用法 <tool>get_time</tool>",
        "calculator：安全计算表达式。用法 <tool>calculator|expression=1+1</tool>",
        "sysinfo：读取基础系统信息。用法 <tool>sysinfo</tool>",
        "list_allowed_paths：列出常用路径。用法 <tool>list_allowed_paths</tool>",
        "web_search：搜索互联网信息。用法 <tool>web_search|query=关键词</tool>",
        "weather：查询城市天气。用法 <tool>weather|city=北京</tool>",
        "--- Robot (ElectronBot) ---",
        "robot_connect：连接 ElectronBot USB。用法 <tool>robot_connect</tool>",
        "robot_disconnect：断开 ElectronBot。用法 <tool>robot_disconnect</tool>",
        "robot_status：读取连接状态 & 舵机角度。用法 <tool>robot_status</tool>",
        "robot_self_test：自检 USB 是否识别设备。用法 <tool>robot_self_test</tool>",
        "robot_display_emoji：在机器人屏幕显示预设表情。用法 <tool>robot_display_emoji|emoji=happy</tool> (选项: happy/excited, sad, angry, surprised/shocked, neutral/idle, disdain/contempt)",
        "robot_set_joints：控制6个舵机角度(度)。各关节有效范围：J1头部[70-95] J2左臂侧摆[-9~3] J3左臂俯仰[-16~117] J4右臂侧摆[133~141] J5右臂俯仰[15~150] J6腰部[0~180]。用法 <tool>robot_set_joints|j1=80|j2=0|j3=50|j4=135|j5=80|j6=90</tool>",
        "--- Robo Actions (preset animations) ---",
        f"robo_play_pose：播放预设动作。用法 <tool>robo_play_pose|pose=wave</tool> (可用: {', '.join(sorted(PRESET_POSES.keys())) if PRESET_POSES else 'wave, anger, disdain, think, nod, shake, celebrate, look_around'})",
        "robo_emotion：同时显示表情+播动作。用法 <tool>robo_emotion|emoji=excited|pose=celebrate</tool>",
    ]
    return "\n".join(lines)
