from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from .storage import Message


ReplyLookup = Callable[[str], Message | None]

CQ_PATTERN = re.compile(r"\[CQ:([a-zA-Z0-9_-]+)((?:,[^\]]*)?)\]")
SUMMARY_EXCLUDED_SEGMENT_TYPES = {"image", "face", "mface", "bface", "marketface"}
IMAGE_SEGMENT_TYPES = {"image"}
STICKER_SEGMENT_TYPES = {"mface", "bface", "marketface"}
MEDIA_PLACEHOLDER_PATTERN = re.compile(
    r"\[(?:图片|表情|表情包)(?::(?:[^\[\]]|\[[^\[\]]*\])*)?\]"
)
MEANINGLESS_SUMMARY_TEXT_PATTERN = re.compile(r"^[\s\[\]()（）【】,，.。!！?？:：;；_\-]+$")


def _cq_unescape(value: str) -> str:
    return (
        value.replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&#44;", ",")
        .replace("&amp;", "&")
    )


def _parse_cq_params(raw_params: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for item in raw_params.lstrip(",").split(","):
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        params[key] = _cq_unescape(value)
    return params


def _raw_cq_to_segments(raw_message: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    cursor = 0
    for match in CQ_PATTERN.finditer(raw_message):
        if match.start() > cursor:
            segments.append(
                {
                    "type": "text",
                    "data": {"text": _cq_unescape(raw_message[cursor : match.start()])},
                }
            )
        segments.append(
            {
                "type": match.group(1),
                "data": _parse_cq_params(match.group(2)),
            }
        )
        cursor = match.end()

    if cursor < len(raw_message):
        segments.append(
            {
                "type": "text",
                "data": {"text": _cq_unescape(raw_message[cursor:])},
            }
        )
    return segments


def _content_preview(content: str, max_chars: int = 80) -> str:
    preview = re.sub(r"\s+", " ", content).strip()
    if len(preview) <= max_chars:
        return preview
    return preview[: max_chars - 1].rstrip() + "…"


def _reply_to_text(data: dict[str, Any], reply_lookup: ReplyLookup | None = None) -> str:
    reply_id = data.get("id") or data.get("message_id")
    if reply_id is not None and reply_lookup is not None:
        replied_message = reply_lookup(str(reply_id))
        if replied_message is not None:
            preview = _content_preview(replied_message.content)
            if preview:
                return f"回复「{replied_message.sender_name}：{preview}」"
            return f"回复「{replied_message.sender_name}」"

    text = data.get("text") or data.get("content")
    if text:
        return f"回复「{_content_preview(str(text))}」"
    if reply_id is not None:
        return f"回复消息 #{reply_id}"
    return "回复消息"


def _at_to_text(data: dict[str, Any]) -> str:
    qq = str(data.get("qq") or "").strip()
    name = (
        data.get("name")
        or data.get("card")
        or data.get("nickname")
        or data.get("text")
    )
    if qq.lower() == "all":
        return "@全体成员"
    if name and qq:
        cleaned_name = str(name).lstrip("@").strip()
        return f"@{cleaned_name}({qq})"
    if name:
        return f"@{str(name).lstrip('@').strip()}"
    if qq:
        return f"@{qq}"
    return "@某人"


def _segment_to_text(segment: dict[str, Any], reply_lookup: ReplyLookup | None = None) -> str:
    segment_type = segment.get("type")
    data = segment.get("data") or {}

    if segment_type == "text":
        return str(data.get("text", ""))
    if segment_type == "at":
        return _at_to_text(data)
    if segment_type == "face":
        return "[表情]"
    if segment_type in STICKER_SEGMENT_TYPES:
        summary = data.get("summary") or data.get("file") or data.get("id") or ""
        return f"[表情包:{summary}]" if summary else "[表情包]"
    if segment_type == "image":
        summary = data.get("summary") or data.get("file") or ""
        return f"[图片:{summary}]" if summary else "[图片]"
    if segment_type == "record":
        return "[语音]"
    if segment_type == "video":
        return "[视频]"
    if segment_type == "file":
        return f"[文件:{data.get('name', '')}]"
    if segment_type == "reply":
        return _reply_to_text(data, reply_lookup)
    if segment_type:
        return f"[{segment_type}]"
    return ""


def _segments_to_text(
    segments: list[Any],
    reply_lookup: ReplyLookup | None = None,
    excluded_segment_types: set[str] | None = None,
) -> str:
    parts = []
    for item in segments:
        if isinstance(item, dict):
            segment_type = str(item.get("type") or "")
            if excluded_segment_types and segment_type in excluded_segment_types:
                continue
            text = _segment_to_text(item, reply_lookup).strip()
            if text:
                parts.append(text)
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _message_to_segments(raw_message: Any, message: Any) -> list[dict[str, Any]]:
    if isinstance(message, list):
        return [item for item in message if isinstance(item, dict)]

    if isinstance(raw_message, str) and raw_message.strip():
        raw_text = raw_message.strip()
        if "[CQ:" in raw_text:
            return _raw_cq_to_segments(raw_text)
        return [{"type": "text", "data": {"text": raw_text}}]

    if isinstance(message, str) and message.strip():
        message_text = message.strip()
        if "[CQ:" in message_text:
            return _raw_cq_to_segments(message_text)
        return [{"type": "text", "data": {"text": message_text}}]

    return []


def special_member_relations(
    raw_message: Any,
    message: Any,
    special_user_id: str | None,
    reply_lookup: ReplyLookup | None = None,
) -> tuple[bool, bool]:
    if not special_user_id:
        return False, False

    mentions_special = False
    replies_to_special = False
    for segment in _message_to_segments(raw_message, message):
        segment_type = str(segment.get("type") or "")
        data = segment.get("data") or {}
        if not isinstance(data, dict):
            continue
        if segment_type == "at" and str(data.get("qq") or "") == special_user_id:
            mentions_special = True
        elif segment_type == "reply" and reply_lookup is not None:
            reply_id = data.get("id") or data.get("message_id")
            if reply_id is None:
                continue
            replied_message = reply_lookup(str(reply_id))
            if replied_message is not None and replied_message.user_id == special_user_id:
                replies_to_special = True
    return mentions_special, replies_to_special


def _strip_media_placeholders(value: str) -> str:
    previous = value
    while True:
        cleaned = MEDIA_PLACEHOLDER_PATTERN.sub("", previous)
        if cleaned == previous:
            break
        previous = cleaned
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned or MEANINGLESS_SUMMARY_TEXT_PATTERN.fullmatch(cleaned):
        return ""
    return cleaned


def _safe_media_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if value.startswith(("http://", "https://")):
        return value
    return ""


def _media_name(data: dict[str, Any]) -> str:
    for key in ("summary", "name", "file", "file_id", "url"):
        value = data.get(key)
        if value:
            return _content_preview(str(value), max_chars=72)
    return ""


def _compact_display_text(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for part in parts:
        if part.get("type") != "text" or not compacted or compacted[-1].get("type") != "text":
            compacted.append(part)
            continue
        compacted[-1]["text"] = f"{compacted[-1].get('text', '')}{part.get('text', '')}"
    return compacted


def message_display_parts(
    raw_message: Any,
    message: Any,
    fallback_content: str = "",
    reply_lookup: ReplyLookup | None = None,
) -> list[dict[str, Any]]:
    display_parts: list[dict[str, Any]] = []
    for segment in _message_to_segments(raw_message, message):
        segment_type = segment.get("type")
        data = segment.get("data") or {}
        if not isinstance(data, dict):
            data = {}

        if segment_type == "text":
            text = str(data.get("text") or "")
            if text:
                display_parts.append({"type": "text", "text": text})
        elif segment_type == "at":
            display_parts.append({"type": "at", "text": _at_to_text(data)})
        elif segment_type == "reply":
            display_parts.append({"type": "reply", "text": _reply_to_text(data, reply_lookup)})
        elif segment_type in IMAGE_SEGMENT_TYPES:
            display_parts.append(
                {
                    "type": "image",
                    "label": "图片",
                    "url": _safe_media_url(data.get("url")),
                    "name": _media_name(data),
                }
            )
        elif segment_type in STICKER_SEGMENT_TYPES:
            display_parts.append(
                {
                    "type": "sticker",
                    "label": "表情包",
                    "url": _safe_media_url(data.get("url")),
                    "name": _media_name(data),
                }
            )
        elif segment_type == "face":
            face_id = data.get("id") or data.get("face_id") or data.get("qq")
            display_parts.append(
                {
                    "type": "face",
                    "label": "QQ 表情",
                    "name": str(face_id) if face_id is not None else "",
                }
            )
        elif segment_type == "record":
            display_parts.append({"type": "attachment", "label": "语音", "name": _media_name(data)})
        elif segment_type == "video":
            display_parts.append({"type": "attachment", "label": "视频", "name": _media_name(data)})
        elif segment_type == "file":
            display_parts.append({"type": "attachment", "label": "文件", "name": _media_name(data)})
        elif segment_type:
            display_parts.append({"type": "attachment", "label": str(segment_type), "name": _media_name(data)})

    if not display_parts and fallback_content:
        display_parts.append({"type": "text", "text": fallback_content})
    return _compact_display_text(display_parts)


def message_to_text(
    raw_message: Any,
    message: Any,
    reply_lookup: ReplyLookup | None = None,
    excluded_segment_types: set[str] | None = None,
) -> str:
    if isinstance(message, list):
        text = _segments_to_text(message, reply_lookup, excluded_segment_types)
        if text or excluded_segment_types:
            return text

    if isinstance(raw_message, str) and raw_message.strip():
        raw_message = raw_message.strip()
        if "[CQ:" in raw_message:
            return _segments_to_text(_raw_cq_to_segments(raw_message), reply_lookup, excluded_segment_types)
        return raw_message

    if isinstance(message, str):
        message = message.strip()
        if "[CQ:" in message:
            return _segments_to_text(_raw_cq_to_segments(message), reply_lookup, excluded_segment_types)
        if excluded_segment_types:
            return _strip_media_placeholders(message)
        return message

    return ""


def message_to_summary_text(
    raw_message: Any,
    message: Any,
    fallback_content: str = "",
    reply_lookup: ReplyLookup | None = None,
) -> str:
    text = message_to_text(
        raw_message,
        message,
        reply_lookup=reply_lookup,
        excluded_segment_types=SUMMARY_EXCLUDED_SEGMENT_TYPES,
    )
    if not text and fallback_content:
        text = fallback_content
    return _strip_media_placeholders(text)


def parse_group_message(
    event: dict[str, Any],
    reply_lookup: ReplyLookup | None = None,
) -> Message | None:
    if event.get("post_type") != "message":
        return None
    if event.get("message_type") != "group":
        return None

    content = message_to_text(event.get("raw_message"), event.get("message"), reply_lookup)
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
