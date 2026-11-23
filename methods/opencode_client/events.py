"""SSE 客户端:订阅 opencode /global/event,产出事件 dict。

opencode server 暴露 /global/event 作为 Server-Sent Events 流。
事件类型:message.part.updated / todo.updated / session.idle / session.error
等等。我们只关心跟目标 sessionID 相关的 part 事件。

每条 event 形如:
    data: {"payload": {"type": "message.part.updated", "properties": {...}, ...}, ...}
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx


def connect_events(
    base_url: str,
    *,
    timeout: float = 6000.0,
) -> Iterator[dict[str, Any]]:
    """订阅 /global/event,持续产出 event dict(raw,未过滤)。

    SSE 协议:每条 event 形如 `data: <json>\\n\\n`。我们忽略 event: / id: 字段。
    网络断开 / 服务端关闭 → StopIteration。
    """
    url = base_url.rstrip("/") + "/global/event"
    with httpx.stream("GET", url, timeout=timeout) as resp:
        resp.raise_for_status()
        buf: str = ""
        for chunk in resp.iter_text():
            if not chunk:
                continue
            buf += chunk
            # SSE 用 \\n\\n 分隔 event
            while "\n\n" in buf:
                block, buf = buf.split("\n\n", 1)
                for line in block.splitlines():
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if not payload:
                        continue
                    try:
                        ev = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    # opencode 在 payload 内部再包一层 {"type", "properties", ...}
                    yield ev


def _unwrap(event: dict[str, Any]) -> dict[str, Any]:
    """从 opencode 的双层包装里抽 inner payload。

    形如 {"payload": {"type": "x", "properties": {...}, ...}, "directory": ...}
    或裸 {"type": "x", ...}
    """
    if "payload" in event and isinstance(event["payload"], dict):
        return event["payload"]
    return event


def is_session_event(event: dict[str, Any], session_id: str) -> bool:
    """该 event 是否跟目标 session 相关(用于过滤全局流)。"""
    inner = _unwrap(event)
    if session_id is None:
        return False
    # 多种位置都可能出现 sessionID
    for key in ("sessionID", "sessionId", "session_id"):
        if inner.get(key) == session_id:
            return True
    props = inner.get("properties") or {}
    if isinstance(props, dict):
        for key in ("sessionID", "sessionId", "session_id"):
            if props.get(key) == session_id:
                return True
    return False


def is_message_part(event: dict[str, Any]) -> bool:
    """是否是 message.part.updated 事件。"""
    return _unwrap(event).get("type") == "message.part.updated"


def extract_part(event: dict[str, Any]) -> dict[str, Any] | None:
    """从 message.part.updated 事件里抽 part dict(没有 → None)。"""
    inner = _unwrap(event)
    if inner.get("type") != "message.part.updated":
        return None
    props = inner.get("properties") or {}
    return props.get("part")


def part_type(part: dict[str, Any]) -> str:
    """part.type,缺失返回空串。"""
    return str(part.get("type") or "")


def part_text(part: dict[str, Any]) -> str:
    """text part 的 text 字段。"""
    return str(part.get("text") or "")


def part_reasoning(part: dict[str, Any]) -> str:
    """reasoning part 的 text 字段(thinking 内容)。"""
    return str(part.get("text") or "")


def part_tool_name(part: dict[str, Any]) -> str:
    """tool part 的 tool 名(比如 'bash' / 'edit' / 'todowrite')。"""
    return str(part.get("tool") or "")


def part_tool_status(part: dict[str, Any]) -> str:
    """tool state.status(running / completed / error)。"""
    state = part.get("state") or {}
    return str(state.get("status") or "")


def part_tokens(part: dict[str, Any]) -> dict[str, int] | None:
    """step-finish part 的 tokens 统计;无则 None。"""
    if part_type(part) != "step-finish":
        return None
    return part.get("tokens") or None


def part_cost(part: dict[str, Any]) -> float | None:
    """step-finish part 的 cost。"""
    if part_type(part) != "step-finish":
        return None
    c = part.get("cost")
    return float(c) if c is not None else None


def is_session_idle(event: dict[str, Any]) -> bool:
    return _unwrap(event).get("type") == "session.idle"


def is_session_error(event: dict[str, Any]) -> bool:
    return _unwrap(event).get("type") in {"session.error", "session.compacted"}


def extract_session_id(event: dict[str, Any]) -> str | None:
    inner = _unwrap(event)
    for key in ("sessionID", "sessionId", "session_id"):
        if inner.get(key):
            return str(inner[key])
    return None
