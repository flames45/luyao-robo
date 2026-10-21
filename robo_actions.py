# -*- coding: utf-8 -*-
"""
Robo action dispatcher — greeting detection, mood analysis, and preset poses.

Analyses user input before the LLM responds, automatically triggering robot
expressions and poses that match the user's tone and intent.
"""
from __future__ import annotations

import queue
import re
import time
import threading
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Greeting / farewell patterns — checked first, highest priority
# ---------------------------------------------------------------------------
GREETING_PATTERNS: list[tuple[str, str, str]] = [
    # (regex pattern, emoji to show, pose name)
    (r"早上好|早安|早啊|早晨|good\s*morning", "excited", "wave"),
    (r"晚上好|晚安|good\s*evening|good\s*night", "neutral", "wave"),
    (r"下午好|good\s*afternoon", "happy", "wave"),
    (r"你好|hello|hi\b|嗨|哈喽|halo", "excited", "wave"),
    (r"再见|拜拜|bye|拜|回头见|下次见|先走了|下了|88", "sad", "wave"),
    (r"我回来了|回来了|我到了", "excited", "wave"),
    (r"我走了|出门了|走了啊", "neutral", "wave"),
]

# ---------------------------------------------------------------------------
# Mood / tone keyword detection
# ---------------------------------------------------------------------------
MOOD_MAP: list[tuple[str, str, list[str]]] = [
    # (mood_name, emoji, [keywords...])
    ("happy", "excited", [
        "哈哈", "开心", "太好了", "好棒", "喜欢", "嘿嘿", "耶",
        "nice", "great", "awesome", "cool", "爱了", "赞", "牛",
        "笑死", "搞笑", "有意思", "好玩", "可爱", "乐",
    ]),
    ("sad", "sad", [
        "难过", "伤心", "哭了", "呜呜", "唉", "不开心",
        "失望", "遗憾", "可惜", "sad", "难受", "郁闷",
        "emo", "崩溃", "撑不住", "好累啊", "好难啊",
    ]),
    ("angry", "angry", [
        "气死", "讨厌", "滚", "烦死了", "怒", "无语",
        "生气", "气人", "火大", "离谱", "有病", "傻逼",
        "我去", "恶心", "什么鬼",
    ]),
    ("surprised", "surprised", [
        "哇", "天哪", "不是吧", "真的假的", "不会吧",
        "居然", "竟然", "我的天", "oh my god", "wow", "震惊",
        "卧槽", "我靠", "wtf", "什么！", "什么？", "什么?!",
        "太强了", "好厉害",
    ]),
    ("thinking", "neutral", [
        "嗯", "emmm", "umm", "让我想想", "等等", "思考",
        "不确定", "可能", "也许", "大概",
    ]),
    ("excited", "excited", [
        "冲冲冲", "加油", "干巴爹", "fighting", "冲",
        "太强了", "牛逼", "厉害", "天才",
    ]),
]

# ---------------------------------------------------------------------------
# Preset poses — keyframe sequences extracted from .NET Emoji JSON files
# PRESET_POSES is defined in skills.py (single source of truth, avoids circular import).
# Imported lazily at function-call time where needed.

# Map moods to poses + emojis
MOOD_TO_ACTION: Dict[str, Tuple[str, str]] = {
    "happy": ("excited", "celebrate"),
    "sad": ("sad", "nod"),
    "angry": ("angry", "anger"),
    "surprised": ("surprised", "look_around"),
    "thinking": ("neutral", "think"),
    "excited": ("excited", "celebrate"),
}


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def detect_greeting(text: str) -> Optional[Tuple[str, str]]:
    """Check if text matches a greeting/farewell pattern.

    Returns (emoji_name, pose_name) or None.
    """
    text_lower = text.lower().strip()
    for pattern, emoji, pose in GREETING_PATTERNS:
        if re.search(pattern, text_lower):
            return (emoji, pose)
    return None


def detect_mood(text: str) -> Optional[Tuple[str, str]]:
    """Analyse text for mood keywords.

    Returns (emoji_name, pose_name) or None if neutral.
    """
    text_lower = text.lower()
    for mood, emoji, keywords in MOOD_MAP:
        for kw in keywords:
            if kw in text_lower:
                action = MOOD_TO_ACTION.get(mood)
                if action:
                    return action
    return None


def analyse(text: str) -> Optional[dict]:
    """Full input analysis — greeting takes priority over mood.

    Returns a dict with 'emoji' and 'pose' keys, or None if nothing detected.
    """
    result = detect_greeting(text)
    if result:
        return {"emoji": result[0], "pose": result[1], "trigger": "greeting"}

    result = detect_mood(text)
    if result:
        return {"emoji": result[0], "pose": result[1], "trigger": "mood"}

    return None


# ---------------------------------------------------------------------------
# Response emoji — always pick an expression for every AI reply
# ---------------------------------------------------------------------------
RESPONSE_EMOJI_KEYWORDS: list[tuple[str, str]] = [
    # (emoji_name, [keywords in AI response...])
    ("excited", ["哈哈", "开心", "太好了", "好棒", "喜欢", "欢迎", "当然", "没问题", "来吧", "加油", "冲", "好呀", "好的呀", "太棒", "厉害", "太强"]),
    ("sad", ["难过", "伤心", "抱歉", "对不起", "可惜", "遗憾", "没办法", "抱抱", "不能", "无法"]),
    ("angry", ["生气", "过分", "讨厌", "可恶", "烦死了", "别闹", "不行", "不允许", "住手"]),
    ("surprised", ["哇", "真的吗", "居然", "竟然", "天哪", "不是吧", "震惊", "什么"]),
    ("neutral", ["嗯", "好的", "明白", "知道", "正在", "检查", "查查看", "看看", "稍等", "等下"]),
    ("disdain", ["哼", "算了", "随便", "无所谓", "不屑", "切"]),
]


def pick_response_emoji(response_text: str) -> str:
    """Pick an emoji for an AI response based on its content.

    Returns an emoji name (always returns something — defaults to 'neutral').
    """
    text = response_text.lower()
    for emoji_name, keywords in RESPONSE_EMOJI_KEYWORDS:
        for kw in keywords:
            if kw in text:
                return emoji_name
    return "neutral"  # default


def dispatch_response_emoji(response_text: str) -> None:
    """Always show a screen expression after the AI responds.

    Called from the chat pipeline after every AI turn.  No pose — just
    the face on the robot screen.
    """
    emoji = pick_response_emoji(response_text)
    dispatch_action({"emoji": emoji, "pose": None, "trigger": "response"})


# ---------------------------------------------------------------------------
# Pose player — executes keyframe sequences on the robot
# ---------------------------------------------------------------------------

logger = __import__('logging').getLogger('robo_actions')

def _get_robot():
    """Lazy-load the robot bridge singleton."""
    try:
        from robot_bridge import get_robot
        return get_robot()
    except Exception:
        return None


def _ensure_connected(robot) -> bool:
    """Make sure the robot is connected, try once if not."""
    if robot is None:
        return False
    if robot.is_connected:
        return True
    try:
        ok = robot.connect()
        if ok:
            robot.start_sync()
            logger.info('Auto-connected to robot for action dispatch')
        return ok
    except Exception as e:
        logger.warning(f'Auto-connect failed: {e}')
        return False


def play_pose(pose_name: str) -> bool:
    """Play a preset pose sequence on the robot."""
    from skills import PRESET_POSES  # lazy import, breaks circular dependency

    robot = _get_robot()
    if not _ensure_connected(robot):
        logger.warning(f'play_pose "{pose_name}" skipped: robot not connected')
        return False

    frames = PRESET_POSES.get(pose_name)
    if not frames:
        logger.warning(f'Unknown pose: {pose_name}')
        return False

    try:
        for frame in frames:
            j1, j2, j3, j4, j5, j6, hold = frame
            robot.set_joints(j1, j2, j3, j4, j5, j6, enable=True)
            time.sleep(hold)
        return True
    except Exception as e:
        logger.error(f'play_pose "{pose_name}" failed: {e}')
        return False


def show_emoji(emoji_name: str) -> bool:
    """Show a preset emoji on the robot screen."""
    robot = _get_robot()
    if not _ensure_connected(robot):
        logger.warning(f'show_emoji "{emoji_name}" skipped: robot not connected')
        return False

    try:
        from skills import robot_display_emoji
        result = robot_display_emoji(emoji=emoji_name)
        ok = "Display emoji" in result
        if not ok:
            logger.warning(f'show_emoji "{emoji_name}" result: {result}')
        return ok
    except Exception as e:
        logger.error(f'show_emoji "{emoji_name}" failed: {e}')
        return False


# ---------------------------------------------------------------------------
# Single-consumer action dispatcher — prevents thread explosion from rapid
# action triggers (e.g. mood analysis + response emoji in quick succession).
# ---------------------------------------------------------------------------
_action_queue: queue.Queue = queue.Queue(maxsize=50)
_consumer_started = threading.Event()


def _action_consumer() -> None:
    """Background thread: drain the action queue and execute robot commands."""
    while True:
        try:
            action = _action_queue.get()
            if action is None:  # shutdown sentinel
                break
            _execute_action(action)
        except Exception:
            pass
        finally:
            try:
                _action_queue.task_done()
            except Exception:
                pass


def _execute_action(action: dict) -> None:
    """Execute a single action synchronously (called from consumer thread)."""
    try:
        emoji = action.get("emoji")
        pose = action.get("pose")
        trigger = action.get("trigger", "?")
        logger.info(f'Dispatch [{trigger}]: emoji={emoji}, pose={pose}')
        if emoji:
            show_emoji(emoji)
        if pose:
            play_pose(pose)
    except Exception as e:
        logger.error(f'Dispatch failed: {e}')


def _ensure_consumer() -> None:
    """Start the consumer thread exactly once."""
    if not _consumer_started.is_set():
        _consumer_started.set()
        t = threading.Thread(target=_action_consumer, daemon=True, name="robo_actions")
        t.start()


def dispatch_action(action: dict) -> None:
    """Enqueue a robot action for background execution. Non-blocking."""
    if action is None:
        return
    _ensure_consumer()
    try:
        _action_queue.put_nowait(action)
    except queue.Full:
        logger.warning("Action queue full, dropping oldest")
        try:
            _action_queue.get_nowait()
            _action_queue.task_done()
            _action_queue.put_nowait(action)
        except (queue.Empty, queue.Full):
            pass
