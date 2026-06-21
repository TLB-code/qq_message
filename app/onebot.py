from __future__ import annotations

import time
from typing import Any

from .storage import Message


def _segment_to_text(segment: dict[str, Any]) -> str:
    segment_type = segment.get("type")
    data = segment.get("data") or {}

    if segment_type == "text":
        return str(data.get("text", ""))
    if segment_type == "at":
        qq = data.get("qq", "")
        return f"@{qq}" if qq else ""
    if segment_type == "face":
        return "[表情]"
    if segment_type == "image":
        return "[图片]"
    if segment_type == "file":
        return f"[文件:{data.get('name', '')}]"
    if segment_type == "reply":
        return "[回复]"
    if segment_type:
        return f"[{segment_type}]"
    return ""


def message_to_text(raw_message: Any, message: Any) -> str:
    if isinstance(raw_message, str) and raw_message.strip():
        return raw_message.strip()

    if isinstance(message, str):
        return message.strip()

    if isinstance(message, list):
        parts = []
        for item in message:
            if isinstance(item, dict):
                parts.append(_segment_to_text(item))
        return " ".join(part for part in parts if part).strip()

    return ""


def parse_group_message(event: dict[str, Any]) -> Message | None:
    if event.get("post_type") != "message":
        return None
    if event.get("message_type") != "group":
        return None

    content = message_to_text(event.get("raw_message"), event.get("message"))
    if not content:
        return None

    sender = event.get("sender") or {}
    sender_name = (
        sender.get("card")
        or sender.get("nickname")
        or sender.get("user_id")
        or event.get("user_id")
        or "unknown"
    )

    message_id = event.get("message_id")
    group_id = event.get("group_id")
    user_id = event.get("user_id")

    if message_id is None or group_id is None or user_id is None:
        return None

    return Message(
        message_id=str(message_id),
        group_id=str(group_id),
        user_id=str(user_id),
        sender_name=str(sender_name),
        content=content,
        timestamp=int(event.get("time") or time.time()),
    )


def group_name_from_event(event: dict[str, Any], group_id: str) -> str:
    group_name = event.get("group_name")
    if group_name:
        return str(group_name)

    group = event.get("group") or {}
    if group.get("group_name"):
        return str(group["group_name"])

    return f"QQ群 {group_id}"

