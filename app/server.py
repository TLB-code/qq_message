from __future__ import annotations

import hmac
import ipaddress
import json
import mimetypes
import queue
import re
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from http.cookies import SimpleCookie
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

from .config import BASE_DIR, load_settings
from .onebot import (
    group_name_from_event,
    is_self_message,
    message_display_parts,
    message_to_summary_text,
    message_to_text,
    message_voice_segments,
    parse_group_message,
    special_member_relations,
)
from .storage import Message, Store
from .summarizer import (
    SUMMARY_PIPELINE_VERSION,
    DeepSeekClient,
    SummarizerError,
    SummaryBlock,
)
from .voice import VoiceArchiveManager


SETTINGS = load_settings()
STORE = Store(SETTINGS.database_path)
VOICE_ARCHIVER = VoiceArchiveManager(
    store=STORE,
    media_root=SETTINGS.voice_media_path,
    source_root=SETTINGS.voice_source_root,
    ffmpeg_path=SETTINGS.voice_ffmpeg_path,
    napcat_api_url=SETTINGS.napcat_onebot_api_url,
    napcat_access_token=SETTINGS.napcat_onebot_access_token,
    enabled=SETTINGS.voice_archive_enabled,
)
SUMMARIZER = DeepSeekClient(
    api_key=SETTINGS.deepseek_api_key,
    base_url=SETTINGS.deepseek_base_url,
    model=SETTINGS.deepseek_model,
    timeout=SETTINGS.deepseek_timeout,
    request_retries=SETTINGS.deepseek_request_retries,
    max_concurrency=SETTINGS.deepseek_max_concurrency,
    special_member_user_id=SETTINGS.special_member_user_id,
    special_member_display_name=SETTINGS.special_member_display_name,
)
STATIC_DIR = BASE_DIR / "frontend" / "dist"
WEBHOOK_LOG_PATH = SETTINGS.database_path.parent / "webhook_events.log"
DEFAULT_SUMMARY_LIMIT = 5000
MAX_SUMMARY_LIMIT = 5000
AUTO_SUMMARY_BATCH_LIMIT = 500
DETAILED_SUMMARY_MAX_MESSAGES = 1000
DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 100
DEFAULT_UNREAD_LIMIT = 100
MAX_UNREAD_LIMIT = 100
DEFAULT_SUMMARY_HISTORY_LIMIT = 5
MAX_SUMMARY_HISTORY_LIMIT = 20
MAX_MEDIA_BYTES = 20 * 1024 * 1024
STATIC_CONTENT_TYPES = {
    ".css": "text/css",
    ".html": "text/html",
    ".js": "text/javascript",
    ".json": "application/json",
    ".mjs": "text/javascript",
    ".svg": "image/svg+xml",
}
MEDIA_ONLY_SUMMARY = "本批消息只有图片、表情等媒体内容，暂未纳入 AI 总结。"
AUTO_SUMMARY_QUEUE: queue.Queue[str] = queue.Queue()
AUTO_SUMMARY_QUEUED_GROUPS: set[str] = set()
AUTO_SUMMARY_QUEUE_LOCK = threading.Lock()
AUTO_SUMMARY_WORKER_STARTED = False
MANUAL_SUMMARY_QUEUE: queue.Queue[str] = queue.Queue()
MANUAL_SUMMARY_WORKER_STARTED = False
SUMMARY_LOCKS: dict[str, threading.Lock] = {}
SUMMARY_LOCKS_LOCK = threading.Lock()
WEB_SESSION_COOKIE = "qq_summary_session"
WEB_SESSION_TOKENS: set[str] = set()
WEB_SESSION_LOCK = threading.Lock()


def raw_event_from_record(record: dict) -> dict:
    try:
        event = json.loads(record.get("raw_json") or "{}")
    except json.JSONDecodeError:
        return {}
    return event if isinstance(event, dict) else {}


def message_from_record(record: dict, content: str | None = None) -> Message:
    return Message(
        message_id=str(record["message_id"]),
        group_id=str(record["group_id"]),
        user_id=str(record["user_id"]),
        sender_name=str(record["sender_name"]),
        content=str(record["content"] if content is None else content),
        timestamp=int(record["timestamp"]),
        position=int(record.get("position") or 0),
    )


def register_voice_media(
    record: dict,
    event: dict,
    store: Store | None = None,
    voice_archiver: VoiceArchiveManager | None = None,
) -> dict[int, dict]:
    store = store or STORE
    existing = {
        int(media["segment_index"]): media
        for media in store.list_message_media(str(record["message_id"]))
    }
    for voice in message_voice_segments(event.get("raw_message"), event.get("message")):
        segment_index = int(voice["segment_index"])
        media = existing.get(segment_index)
        if media is None:
            media = store.ensure_message_media(
                message_id=str(record["message_id"]),
                group_id=str(record["group_id"]),
                segment_index=segment_index,
                media_type="voice",
                source_name=str(voice.get("source_name") or voice.get("display_name") or ""),
                created_at=int(record["timestamp"]),
            )
            existing[segment_index] = media
        if voice_archiver is not None and media.get("status") in {"pending", "processing"}:
            voice_archiver.enqueue(int(media["id"]))
    return existing


def message_payload_from_record(
    record: dict,
    store: Store | None = None,
    voice_archiver: VoiceArchiveManager | None = None,
) -> dict:
    store = store or STORE
    event = raw_event_from_record(record)
    message = message_from_record(record)
    payload = {
        "message_id": message.message_id,
        "group_id": message.group_id,
        "user_id": message.user_id,
        "sender_name": message.sender_name,
        "is_self": is_self_message(event, SETTINGS.self_user_id),
        "content": message.content,
        "timestamp": message.timestamp,
    }
    display_parts = message_display_parts(
        event.get("raw_message"),
        event.get("message"),
        fallback_content=payload["content"],
        reply_lookup=STORE.get_message,
    )
    voice_media: dict[int, dict] = {}
    if any(part.get("type") == "audio" for part in display_parts):
        if voice_archiver is not None and voice_archiver.enabled:
            voice_media = register_voice_media(
                record,
                event,
                store=store,
                voice_archiver=voice_archiver,
            )
        else:
            voice_media = {
                int(media["segment_index"]): media
                for media in store.list_message_media(str(record["message_id"]))
            }
    for part in display_parts:
        media_url = part.get("url")
        if isinstance(media_url, str) and is_allowed_media_url(media_url):
            part["proxy_url"] = media_proxy_url(media_url)
        if part.get("type") != "audio":
            continue
        media = voice_media.get(int(part.get("segment_index") or 0))
        if media is None:
            part["status"] = "unavailable"
            continue
        part["media_id"] = int(media["id"])
        part["status"] = str(media.get("status") or "pending")
        part["playback_url"] = f"/api/voice/{media['id']}"
        part["retry_url"] = f"/api/voice/{media['id']}/retry"
        if part["status"] == "ready" and voice_archiver is not None:
            playback_path = voice_archiver.resolve_playback_path(media)
            if playback_path is None or not playback_path.is_file():
                store.reset_message_media_for_retry(int(media["id"]), int(time.time()))
                voice_archiver.enqueue(int(media["id"]))
                part["status"] = "pending"
    payload["display_parts"] = display_parts
    return payload


def parse_http_byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    if not value:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or size <= 0:
        raise ValueError("Invalid byte range")
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise ValueError("Invalid byte range")
    if not start_text:
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise ValueError("Invalid byte range")
        start = max(size - suffix_length, 0)
        end = size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    if start >= size or end < start:
        raise ValueError("Unsatisfiable byte range")
    return start, min(end, size - 1)


def is_allowed_media_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False

    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return False

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True

    return not (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def media_proxy_url(value: str) -> str:
    return f"/api/media?url={quote(value, safe='')}"


def summary_message_from_record(record: dict) -> Message | None:
    event = raw_event_from_record(record)
    content = message_to_summary_text(
        event.get("raw_message"),
        event.get("message"),
        fallback_content=str(record.get("content") or ""),
        reply_lookup=STORE.get_message,
    )
    if not content:
        return None
    event = raw_event_from_record(record)
    mentions_special, replies_to_special = special_member_relations(
        event.get("raw_message"),
        event.get("message"),
        SETTINGS.special_member_user_id,
        reply_lookup=STORE.get_message,
    )
    message = message_from_record(record, content=content)
    return Message(
        **{
            **message.__dict__,
            "is_special_sender": bool(
                SETTINGS.special_member_user_id
                and message.user_id == SETTINGS.special_member_user_id
            ),
            "mentions_special": mentions_special,
            "replies_to_special": replies_to_special,
        }
    )


def parse_history_date(value: str) -> tuple[int, int, str]:
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Invalid date, expected YYYY-MM-DD") from exc

    local_timezone = datetime.now().astimezone().tzinfo
    started_at = datetime.combine(day, datetime.min.time(), tzinfo=local_timezone)
    ended_at = started_at + timedelta(days=1)
    return int(started_at.timestamp()), int(ended_at.timestamp()), day.isoformat()


def query_bool(query: dict[str, list[str]], name: str, default: bool = False) -> bool:
    values = query.get(name)
    if not values:
        return default
    return values[0].strip().lower() in {"1", "true", "yes", "on"}


def parse_manual_summary_limit(value: object) -> int:
    if value is None or value == "":
        return DEFAULT_SUMMARY_LIMIT
    if isinstance(value, bool):
        raise ValueError(f"Summary limit must be an integer from 1 to {MAX_SUMMARY_LIMIT}")
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Summary limit must be an integer from 1 to {MAX_SUMMARY_LIMIT}") from exc
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"Summary limit must be an integer from 1 to {MAX_SUMMARY_LIMIT}")
    if not 1 <= limit <= MAX_SUMMARY_LIMIT:
        raise ValueError(f"Summary limit must be an integer from 1 to {MAX_SUMMARY_LIMIT}")
    return limit


def parse_manual_summary_mode(value: object) -> str:
    if value is None or value == "":
        return "detailed"
    mode = str(value).strip().lower()
    if mode not in {"detailed", "fast"}:
        raise ValueError("Summary mode must be detailed or fast")
    return mode


def summary_task_payload(task: dict, events: list[dict] | None = None) -> dict:
    return {
        "task_id": str(task["task_id"]),
        "group_id": str(task["group_id"]),
        "requested_limit": int(task["requested_limit"]),
        "status": str(task["status"]),
        "stage": str(task.get("stage") or task["status"]),
        "mode": str(task.get("mode") or "detailed"),
        "total_messages": int(task.get("total_messages") or 0),
        "total_chunks": int(task.get("total_chunks") or 0),
        "completed_chunks": int(task.get("completed_chunks") or 0),
        "progress_percent": int(task.get("progress_percent") or 0),
        "phase_current": int(task.get("phase_current") or 0),
        "phase_total": int(task.get("phase_total") or 0),
        "phase_label": str(task.get("phase_label") or ""),
        "summary_id": task.get("summary_id"),
        "message_count": task.get("message_count"),
        "error": task.get("error"),
        "created_at": int(task["created_at"]),
        "started_at": task.get("started_at"),
        "completed_at": task.get("completed_at"),
        "events": [
            {
                "id": int(event["id"]),
                "level": str(event["level"]),
                "stage": str(event["stage"]),
                "batch_current": int(event.get("batch_current") or 0),
                "batch_total": int(event.get("batch_total") or 0),
                "message": str(event["message"]),
                "created_at": int(event["created_at"]),
            }
            for event in (events or [])
        ],
    }


def public_summary_error(exc: Exception) -> str:
    message = str(exc).strip() or exc.__class__.__name__
    for secret in (SETTINGS.deepseek_api_key, SETTINGS.webhook_token, SETTINGS.web_password):
        if secret:
            message = message.replace(secret, "***")
    return message[:500]


def web_auth_enabled() -> bool:
    return bool(SETTINGS.web_password)


def create_web_session() -> str:
    token = secrets.token_urlsafe(32)
    with WEB_SESSION_LOCK:
        WEB_SESSION_TOKENS.add(token)
    return token


def revoke_web_session(token: str | None) -> None:
    if not token:
        return
    with WEB_SESSION_LOCK:
        WEB_SESSION_TOKENS.discard(token)


def is_valid_web_session(token: str | None) -> bool:
    if not token:
        return False
    with WEB_SESSION_LOCK:
        return token in WEB_SESSION_TOKENS


def webhook_token_matches(value: str | None) -> bool:
    expected = SETTINGS.webhook_token
    if not expected:
        return True
    return bool(value and hmac.compare_digest(value, expected))


def summary_lock_for_group(group_id: str) -> threading.Lock:
    with SUMMARY_LOCKS_LOCK:
        lock = SUMMARY_LOCKS.get(group_id)
        if lock is None:
            lock = threading.Lock()
            SUMMARY_LOCKS[group_id] = lock
        return lock


def summarize_group_messages(group_id: str, limit: int, mark_read: bool = True) -> dict:
    group = STORE.get_group(group_id)
    if not group:
        raise ValueError("Group not found")

    records = STORE.get_unread_message_records(group_id, limit=limit)
    positioned_records = [
        {**record, "position": position}
        for position, record in enumerate(records, start=1)
    ]
    messages = [message_from_record(record) for record in positioned_records]
    summary_messages = [
        message
        for message in (summary_message_from_record(record) for record in positioned_records)
        if message is not None
    ]
    if not messages:
        raise ValueError("No unread messages to summarize")

    if summary_messages:
        summary = SUMMARIZER.summarize(
            group["group_name"],
            summary_messages,
            source_messages=messages,
        )
    else:
        summary = MEDIA_ONLY_SUMMARY

    created_at = int(time.time())
    summary_id = STORE.save_summary(
        group_id=group_id,
        messages=messages,
        summary=summary,
        model=SETTINGS.deepseek_model,
        created_at=created_at,
        mark_read=mark_read,
    )
    return {
        "summary_id": summary_id,
        "summary": summary,
        "message_count": len(messages),
        "model": SETTINGS.deepseek_model,
    }


def enqueue_auto_summary_if_needed(group_id: str) -> bool:
    if not SETTINGS.auto_summary_enabled:
        return False
    if not STORE.is_group_auto_summary_enabled(group_id):
        return False
    if STORE.get_active_summary_task(group_id):
        return False

    unread_count = STORE.count_unread_message_records(group_id)
    if unread_count < SETTINGS.auto_summary_threshold:
        return False

    with AUTO_SUMMARY_QUEUE_LOCK:
        if group_id in AUTO_SUMMARY_QUEUED_GROUPS:
            return False
        AUTO_SUMMARY_QUEUED_GROUPS.add(group_id)
        AUTO_SUMMARY_QUEUE.put(group_id)
        return True


def auto_summary_worker() -> None:
    while True:
        group_id = AUTO_SUMMARY_QUEUE.get()
        try:
            lock = summary_lock_for_group(group_id)
            with lock:
                while (
                    SETTINGS.auto_summary_enabled
                    and STORE.is_group_auto_summary_enabled(group_id)
                    and STORE.count_unread_message_records(group_id) >= SETTINGS.auto_summary_threshold
                ):
                    try:
                        result = summarize_group_messages(
                            group_id,
                            limit=AUTO_SUMMARY_BATCH_LIMIT,
                            mark_read=True,
                        )
                    except SummarizerError as exc:
                        print(f"Auto summary failed for group {group_id}: {exc}")
                        break
                    except ValueError as exc:
                        print(f"Auto summary skipped for group {group_id}: {exc}")
                        break
                    except Exception as exc:
                        print(f"Auto summary crashed for group {group_id}: {exc}")
                        break
                    print(
                        "Auto summarized "
                        f"{result['message_count']} messages for group {group_id} "
                        f"as summary #{result['summary_id']}"
                    )
        finally:
            with AUTO_SUMMARY_QUEUE_LOCK:
                AUTO_SUMMARY_QUEUED_GROUPS.discard(group_id)
            AUTO_SUMMARY_QUEUE.task_done()


def start_auto_summary_worker() -> None:
    global AUTO_SUMMARY_WORKER_STARTED
    if AUTO_SUMMARY_WORKER_STARTED:
        return
    AUTO_SUMMARY_WORKER_STARTED = True
    worker = threading.Thread(target=auto_summary_worker, name="auto-summary-worker", daemon=True)
    worker.start()


def process_manual_summary_task(task_id: str) -> dict | None:
    task = STORE.get_summary_task(task_id)
    if not task or task["status"] != "queued":
        return task

    try:
        STORE.mark_summary_task_running(task_id, int(time.time()))
        STORE.add_summary_task_event(
            task_id,
            level="info",
            stage="preparing",
            message="后台任务开始执行。",
            created_at=int(time.time()),
        )
        lock = summary_lock_for_group(str(task["group_id"]))
        with lock:
            records = STORE.list_summary_task_message_records(task_id)
            if not records:
                records = STORE.get_unread_message_records(
                    str(task["group_id"]),
                    limit=int(task["requested_limit"]),
                )
                if not records:
                    raise ValueError("No unread messages to summarize")
                STORE.save_summary_task_message_snapshot(task_id, records)
                records = STORE.list_summary_task_message_records(task_id)

            refreshed_task = STORE.get_summary_task(task_id) or task
            expected_records = int(refreshed_task.get("total_messages") or len(records))
            if len(records) != expected_records:
                raise ValueError("Some messages in the summary task snapshot are no longer available")

            requested_mode = str(refreshed_task.get("mode") or "detailed")
            summary_mode = (
                requested_mode
                if len(records) <= DETAILED_SUMMARY_MAX_MESSAGES
                else "fast"
            )
            STORE.set_summary_task_mode(task_id, summary_mode)
            mode_label = "精细模式" if summary_mode == "detailed" else "快速模式"
            STORE.update_summary_task_progress(
                task_id,
                stage="preparing",
                progress_percent=0,
                phase_label="准备总结",
            )
            STORE.add_summary_task_event(
                task_id,
                level="info",
                stage="preparing",
                message=f"已固定 {len(records)} 条消息，使用{mode_label}。",
                created_at=int(time.time()),
            )

            group = STORE.get_group(str(task["group_id"]))
            if not group:
                raise ValueError("Group not found")
            messages = [message_from_record(record) for record in records]
            summary_messages = [
                message
                for message in (summary_message_from_record(record) for record in records)
                if message is not None
            ]

            if summary_messages:
                saved_blocks = {
                    int(row["chunk_index"]): SummaryBlock(
                        title=str(row["title"]),
                        message_count=int(row["message_count"]),
                        time_range=str(row["time_range"]),
                        content=str(row["summary"]),
                    )
                    for row in STORE.list_summary_task_chunks(task_id)
                }

                def save_plan(total_chunks: int) -> None:
                    STORE.set_summary_task_plan(task_id, total_chunks)

                def save_chunk(chunk_index: int, block: SummaryBlock) -> None:
                    STORE.save_summary_task_chunk(
                        task_id,
                        chunk_index=chunk_index,
                        title=block.title,
                        message_count=block.message_count,
                        time_range=block.time_range,
                        summary=block.content,
                        completed_at=int(time.time()),
                    )

                phase_labels = {
                    "summarizing": "生成分块摘要",
                    "merging": "树状合并摘要",
                    "reviewing": "分栏目复核",
                    "finalizing": "整理最终总结",
                }

                def save_progress(
                    stage: str,
                    level: str,
                    message: str,
                    current: int,
                    total: int,
                    percent: int,
                ) -> None:
                    STORE.update_summary_task_progress(
                        task_id,
                        stage=stage,
                        progress_percent=percent,
                        phase_current=current,
                        phase_total=total,
                        phase_label=phase_labels.get(stage, stage),
                    )
                    STORE.add_summary_task_event(
                        task_id,
                        level=level,
                        stage=stage,
                        message=message,
                        batch_current=current,
                        batch_total=total,
                        created_at=int(time.time()),
                    )

                summarize_method = (
                    SUMMARIZER.summarize
                    if summary_mode == "detailed"
                    else SUMMARIZER.summarize_fast
                )
                summary = summarize_method(
                    str(group["group_name"]),
                    summary_messages,
                    existing_blocks=saved_blocks,
                    plan_callback=save_plan,
                    chunk_callback=save_chunk,
                    stage_callback=lambda stage: STORE.set_summary_task_stage(task_id, stage),
                    progress_callback=save_progress,
                    source_messages=messages,
                )
            else:
                STORE.set_summary_task_plan(task_id, 0)
                summary = MEDIA_ONLY_SUMMARY
                STORE.update_summary_task_progress(
                    task_id,
                    stage="finalizing",
                    progress_percent=98,
                    phase_current=1,
                    phase_total=1,
                    phase_label="整理最终总结",
                )
                STORE.add_summary_task_event(
                    task_id,
                    level="success",
                    stage="finalizing",
                    message="本批只有媒体消息，已生成媒体提示摘要。",
                    batch_current=1,
                    batch_total=1,
                    created_at=int(time.time()),
                )

            STORE.update_summary_task_progress(
                task_id,
                stage="saving",
                progress_percent=99,
                phase_current=0,
                phase_total=1,
                phase_label="保存总结",
            )
            STORE.add_summary_task_event(
                task_id,
                level="info",
                stage="saving",
                message="最终总结已生成，正在保存。",
                created_at=int(time.time()),
            )
            summary_id = STORE.save_summary(
                group_id=str(task["group_id"]),
                messages=messages,
                summary=summary,
                model=SETTINGS.deepseek_model,
                created_at=int(time.time()),
                mark_read=bool(task["mark_read"]),
                task_id=task_id,
            )
            STORE.add_summary_task_event(
                task_id,
                level="success",
                stage="completed",
                message=f"总结已完成，共处理 {len(messages)} 条消息。",
                batch_current=1,
                batch_total=1,
                created_at=int(time.time()),
            )
            result = {
                "summary_id": summary_id,
                "message_count": len(messages),
            }
        enqueue_auto_summary_if_needed(str(task["group_id"]))
    except Exception as exc:
        error_message = public_summary_error(exc)
        current_task = STORE.get_summary_task(task_id)
        if not current_task or current_task["status"] != "completed":
            STORE.fail_summary_task(task_id, error_message, int(time.time()))
            STORE.add_summary_task_event(
                task_id,
                level="error",
                stage="failed",
                message=f"任务失败：{error_message}",
                created_at=int(time.time()),
            )
        print(f"Manual summary task {task_id} failed: {exc}")
    return STORE.get_summary_task(task_id)


def manual_summary_worker() -> None:
    while True:
        task_id = MANUAL_SUMMARY_QUEUE.get()
        try:
            process_manual_summary_task(task_id)
        finally:
            MANUAL_SUMMARY_QUEUE.task_done()


def start_manual_summary_worker() -> None:
    global MANUAL_SUMMARY_WORKER_STARTED
    if MANUAL_SUMMARY_WORKER_STARTED:
        return
    MANUAL_SUMMARY_WORKER_STARTED = True
    worker = threading.Thread(target=manual_summary_worker, name="manual-summary-worker", daemon=True)
    worker.start()


def enqueue_existing_auto_summaries() -> int:
    count = 0
    for group in STORE.list_groups():
        if enqueue_auto_summary_if_needed(str(group["group_id"])):
            count += 1
    return count


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "QQSummary/0.1"

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/auth/status":
            self._handle_auth_status()
            return
        if parsed.path in ("", "/"):
            self._serve_static("index.html")
            return
        if parsed.path.startswith("/assets/"):
            self._serve_static(parsed.path.lstrip("/"))
            return
        if parsed.path.startswith("/static/"):
            self._serve_static(parsed.path.removeprefix("/static/"))
            return
        if not self._require_web_auth(parsed.path):
            return
        if parsed.path.startswith("/api/voice/"):
            self._handle_voice_get(parsed.path)
            return
        if parsed.path == "/api/media":
            self._handle_media_get(parse_qs(parsed.query))
            return
        if parsed.path == "/api/groups":
            self._json(HTTPStatus.OK, {"groups": STORE.list_groups()})
            return
        if parsed.path.startswith("/api/groups/"):
            self._handle_group_get(parsed.path, parse_qs(parsed.query))
            return
        if parsed.path == "/api/health":
            self._json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "database": str(SETTINGS.database_path),
                    "model": SETTINGS.deepseek_model,
                    "auto_summary_enabled": SETTINGS.auto_summary_enabled,
                    "auto_summary_threshold": SETTINGS.auto_summary_threshold,
                },
            )
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _handle_media_get(self, query: dict[str, list[str]]) -> None:
        media_url = (query.get("url") or [""])[0]
        if not media_url or not is_allowed_media_url(media_url):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid media URL"})
            return

        request = Request(
            media_url,
            headers={
                "User-Agent": "QQSummary/0.1",
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            },
        )
        try:
            with urlopen(request, timeout=10) as response:
                content_type = response.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0]
                content_length = int(response.headers.get("Content-Length") or "0")
                if content_length > MAX_MEDIA_BYTES:
                    self._json(HTTPStatus.PAYLOAD_TOO_LARGE, {"error": "Media file is too large"})
                    return

                content = response.read(MAX_MEDIA_BYTES + 1)
        except HTTPError as exc:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": f"Media request failed: {exc.code}"})
            return
        except (TimeoutError, URLError, OSError) as exc:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": f"Media request failed: {exc}"})
            return

        if len(content) > MAX_MEDIA_BYTES:
            self._json(HTTPStatus.PAYLOAD_TOO_LARGE, {"error": "Media file is too large"})
            return
        if not content_type.startswith(("image/", "video/")):
            content_type = "application/octet-stream"

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.end_headers()
        self.wfile.write(content)

    def _handle_voice_get(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[0:2] != ["api", "voice"]:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        try:
            media_id = int(parts[2])
        except (TypeError, ValueError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid voice media id"})
            return

        media = STORE.get_message_media(media_id)
        if not media or media.get("media_type") != "voice":
            self._json(HTTPStatus.NOT_FOUND, {"error": "Voice media not found"})
            return
        if media.get("status") != "ready":
            VOICE_ARCHIVER.enqueue(media_id)
            self._json(
                HTTPStatus.ACCEPTED,
                {"error": "Voice is still being prepared", "status": media.get("status")},
            )
            return

        file_path = VOICE_ARCHIVER.resolve_playback_path(media)
        if file_path is None or not file_path.is_file():
            STORE.reset_message_media_for_retry(media_id, int(time.time()))
            VOICE_ARCHIVER.enqueue(media_id)
            self._json(HTTPStatus.ACCEPTED, {"error": "Voice file is being restored"})
            return
        size = file_path.stat().st_size
        try:
            byte_range = parse_http_byte_range(self.headers.get("Range"), size)
        except ValueError:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            return

        if byte_range is None:
            start, end = 0, size - 1
            status = HTTPStatus.OK
        else:
            start, end = byte_range
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", media.get("mime_type") or "audio/mpeg")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if byte_range is not None:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Disposition", "inline")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        with file_path.open("rb") as file:
            file.seek(start)
            remaining = length
            while remaining > 0:
                chunk = file.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/auth/login":
            self._handle_auth_login()
            return
        if parsed.path == "/api/auth/logout":
            self._handle_auth_logout()
            return
        if parsed.path == "/webhook/onebot":
            self._handle_onebot_webhook(parse_qs(parsed.query))
            return
        if not self._require_web_auth(parsed.path):
            return
        if parsed.path.startswith("/api/voice/") and parsed.path.endswith("/retry"):
            self._handle_voice_retry(parsed.path)
            return
        if parsed.path.startswith("/api/groups/"):
            self._handle_group_post(parsed.path)
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _handle_voice_retry(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != 4 or parts[0:2] != ["api", "voice"] or parts[3] != "retry":
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        try:
            media_id = int(parts[2])
        except (TypeError, ValueError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid voice media id"})
            return
        media = STORE.get_message_media(media_id)
        if not media or media.get("media_type") != "voice":
            self._json(HTTPStatus.NOT_FOUND, {"error": "Voice media not found"})
            return
        STORE.reset_message_media_for_retry(media_id, int(time.time()))
        queued = VOICE_ARCHIVER.enqueue(media_id)
        self._json(
            HTTPStatus.ACCEPTED,
            {"ok": True, "queued": queued, "media_id": media_id},
        )

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if not self._require_web_auth(parsed.path):
            return
        if parsed.path.startswith("/api/groups/"):
            self._handle_group_delete(parsed.path, parse_qs(parsed.query))
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _handle_auth_status(self) -> None:
        authenticated = (not web_auth_enabled()) or is_valid_web_session(self._web_session_token())
        self._json(
            HTTPStatus.OK,
            {
                "auth_required": web_auth_enabled(),
                "authenticated": authenticated,
            },
        )

    def _handle_auth_login(self) -> None:
        if not web_auth_enabled():
            self._json(HTTPStatus.OK, {"ok": True, "auth_required": False, "authenticated": True})
            return

        body = self._read_json(default={})
        password = str(body.get("password") or "")
        if not hmac.compare_digest(password, SETTINGS.web_password or ""):
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Invalid password"})
            return

        session_token = create_web_session()
        self._json(
            HTTPStatus.OK,
            {"ok": True, "auth_required": True, "authenticated": True},
            cookies=[self._session_cookie(session_token)],
        )

    def _handle_auth_logout(self) -> None:
        revoke_web_session(self._web_session_token())
        self._json(
            HTTPStatus.OK,
            {"ok": True},
            cookies=[self._expired_session_cookie()],
        )

    def _require_web_auth(self, path: str) -> bool:
        if not web_auth_enabled():
            return True
        if path.startswith("/api/auth/"):
            return True
        if is_valid_web_session(self._web_session_token()):
            return True
        if path.startswith("/api/"):
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Login required"})
        else:
            self._serve_static("index.html")
        return False

    def _web_session_token(self) -> str | None:
        cookie_header = self.headers.get("Cookie")
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        morsel = cookie.get(WEB_SESSION_COOKIE)
        return morsel.value if morsel else None

    def _session_cookie(self, token: str) -> str:
        secure = " Secure;" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
        return f"{WEB_SESSION_COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000;{secure}"

    def _expired_session_cookie(self) -> str:
        return f"{WEB_SESSION_COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0;"

    def _handle_group_get(self, path: str, query: dict[str, list[str]]) -> None:
        parts = path.strip("/").split("/")
        if len(parts) not in (3, 4, 5) or parts[0] != "api" or parts[1] != "groups":
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        group_id = parts[2]
        if group_id.endswith("/"):
            group_id = group_id[:-1]

        if len(parts) == 5:
            if parts[3] == "summary-tasks":
                self._handle_summary_task_get(group_id, parts[4])
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        if len(parts) == 4:
            if parts[3] == "unread":
                self._handle_unread_get(group_id, query)
                return
            if parts[3] == "history":
                self._handle_history_get(group_id, query)
                return
            if parts[3] == "summaries":
                self._handle_summaries_get(group_id, query)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        group = STORE.get_group(group_id)
        if not group:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Group not found"})
            return

        limit = int((query.get("limit") or ["50"])[0])
        self._json(
            HTTPStatus.OK,
            {
                "group": group,
                "unread": [
                    message_payload_from_record(record, voice_archiver=VOICE_ARCHIVER)
                    for record in STORE.get_unread_message_records(group_id, limit=limit)
                ],
                "recent": [
                    message_payload_from_record(record, voice_archiver=VOICE_ARCHIVER)
                    for record in STORE.list_recent_message_records(group_id, limit=limit)
                ],
                "summaries": STORE.list_summaries(group_id),
            },
        )

    def _handle_summary_task_get(self, group_id: str, task_id: str) -> None:
        if not STORE.get_group(group_id):
            self._json(HTTPStatus.NOT_FOUND, {"error": "Group not found"})
            return

        if task_id == "active":
            task = STORE.get_active_summary_task(group_id)
            self._json(
                HTTPStatus.OK,
                {
                    "task": summary_task_payload(
                        task,
                        STORE.list_summary_task_events(str(task["task_id"])),
                    )
                    if task
                    else None
                },
            )
            return

        task = STORE.get_summary_task(task_id, group_id=group_id)
        if not task:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Summary task not found"})
            return
        self._json(
            HTTPStatus.OK,
            {
                "task": summary_task_payload(
                    task,
                    STORE.list_summary_task_events(str(task["task_id"])),
                )
            },
        )

    def _handle_unread_get(self, group_id: str, query: dict[str, list[str]]) -> None:
        group = STORE.get_group(group_id)
        if not group:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Group not found"})
            return

        try:
            requested_limit = int((query.get("limit") or [str(DEFAULT_UNREAD_LIMIT)])[0])
        except (TypeError, ValueError):
            requested_limit = DEFAULT_UNREAD_LIMIT
        limit = min(max(requested_limit, 1), MAX_UNREAD_LIMIT)

        before_timestamp: int | None = None
        before_message_id: str | None = None
        if query.get("before_timestamp") and query.get("before_message_id"):
            try:
                before_timestamp = int(query["before_timestamp"][0])
                before_message_id = str(query["before_message_id"][0])
            except (TypeError, ValueError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid unread cursor"})
                return

        records = STORE.list_unread_message_page_records(
            group_id,
            limit=limit + 1,
            before_timestamp=before_timestamp,
            before_message_id=before_message_id,
        )
        has_more = len(records) > limit
        records = records[-limit:]
        messages = [
            message_payload_from_record(record, voice_archiver=VOICE_ARCHIVER)
            for record in records
        ]
        next_cursor = None
        if has_more and messages:
            first = messages[0]
            next_cursor = {
                "before_timestamp": first["timestamp"],
                "before_message_id": first["message_id"],
            }

        self._json(
            HTTPStatus.OK,
            {
                "group": group,
                "messages": messages,
                "total_count": int(group.get("unread_count") or 0),
                "has_more": has_more,
                "next_cursor": next_cursor,
                "limit": limit,
            },
        )

    def _handle_summaries_get(self, group_id: str, query: dict[str, list[str]]) -> None:
        group = STORE.get_group(group_id)
        if not group:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Group not found"})
            return

        try:
            requested_limit = int((query.get("limit") or [str(DEFAULT_SUMMARY_HISTORY_LIMIT)])[0])
        except (TypeError, ValueError):
            requested_limit = DEFAULT_SUMMARY_HISTORY_LIMIT
        limit = min(max(requested_limit, 1), MAX_SUMMARY_HISTORY_LIMIT)
        include_total = query_bool(query, "include_total", default=True)

        before_id: int | None = None
        if query.get("before_id"):
            try:
                before_id = int(query["before_id"][0])
            except (TypeError, ValueError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid summary cursor"})
                return

        records = STORE.list_summaries(group_id, limit=limit + 1, before_id=before_id)
        has_more = len(records) > limit
        summaries = records[:limit]
        next_cursor = None
        if has_more and summaries:
            next_cursor = {"before_id": summaries[-1]["id"]}

        payload = {
            "group": group,
            "summaries": summaries,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "limit": limit,
        }
        if include_total:
            payload["total_count"] = STORE.count_summaries(group_id)
        self._json(HTTPStatus.OK, payload)

    def _handle_history_get(self, group_id: str, query: dict[str, list[str]]) -> None:
        group = STORE.get_group(group_id)
        if not group:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Group not found"})
            return

        try:
            requested_limit = int((query.get("limit") or [str(DEFAULT_HISTORY_LIMIT)])[0])
        except (TypeError, ValueError):
            requested_limit = DEFAULT_HISTORY_LIMIT
        limit = min(max(requested_limit, 1), MAX_HISTORY_LIMIT)
        include_total = query_bool(query, "include_total", default=True)

        before_timestamp: int | None = None
        before_message_id: str | None = None
        start_timestamp: int | None = None
        end_timestamp: int | None = None
        selected_date: str | None = None
        if query.get("before_timestamp") and query.get("before_message_id"):
            try:
                before_timestamp = int(query["before_timestamp"][0])
                before_message_id = str(query["before_message_id"][0])
            except (TypeError, ValueError):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid history cursor"})
                return
        if query.get("date"):
            try:
                start_timestamp, end_timestamp, selected_date = parse_history_date(query["date"][0])
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return

        records = STORE.list_history_message_records(
            group_id,
            limit=limit + 1,
            before_timestamp=before_timestamp,
            before_message_id=before_message_id,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )
        has_more = len(records) > limit
        records = records[-limit:]
        messages = [
            message_payload_from_record(record, voice_archiver=VOICE_ARCHIVER)
            for record in records
        ]
        next_cursor = None
        if has_more and messages:
            first = messages[0]
            next_cursor = {
                "before_timestamp": first["timestamp"],
                "before_message_id": first["message_id"],
            }

        payload = {
            "group": group,
            "messages": messages,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "limit": limit,
            "date": selected_date,
        }
        if include_total:
            payload["total_count"] = STORE.count_message_records(
                group_id,
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
            )
        self._json(HTTPStatus.OK, payload)

    def _handle_group_delete(self, path: str, query: dict[str, list[str]]) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != 4 or parts[0] != "api" or parts[1] != "groups" or parts[3] != "history":
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        group_id = parts[2]
        group = STORE.get_group(group_id)
        if not group:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Group not found"})
            return
        if not query.get("date"):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Missing date"})
            return

        try:
            start_timestamp, end_timestamp, selected_date = parse_history_date(query["date"][0])
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        media_rows = STORE.list_message_media_in_range(group_id, start_timestamp, end_timestamp)
        deleted_count = STORE.delete_message_records_in_range(group_id, start_timestamp, end_timestamp)
        deleted_media_files = VOICE_ARCHIVER.delete_files(media_rows) if deleted_count else 0
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "group": group,
                "date": selected_date,
                "deleted_count": deleted_count,
                "deleted_media_files": deleted_media_files,
            },
        )

    def _handle_group_post(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if parts[0:2] != ["api", "groups"]:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        if len(parts) == 6 and parts[3] == "summaries" and parts[5] == "read":
            self._mark_summary_read(parts[2], parts[4])
            return

        if len(parts) != 4:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        group_id, action = parts[2], parts[3]
        if action == "summarize":
            self._summarize_group(group_id)
            return
        if action == "auto-summary":
            self._set_group_auto_summary(group_id)
            return
        if action == "mark-read":
            try:
                result = STORE.mark_read(group_id, now=int(time.time()))
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._json(HTTPStatus.OK, {"ok": True, **result})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _set_group_auto_summary(self, group_id: str) -> None:
        group = STORE.get_group(group_id)
        if not group:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Group not found"})
            return

        body = self._read_json(default={})
        enabled = bool(body.get("enabled"))
        updated_group = STORE.set_group_auto_summary_enabled(group_id, enabled)
        if updated_group is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Group not found"})
            return

        auto_summary_enqueued = False
        if enabled:
            auto_summary_enqueued = enqueue_auto_summary_if_needed(group_id)

        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "group": updated_group,
                "auto_summary_enqueued": auto_summary_enqueued,
            },
        )

    def _mark_summary_read(self, group_id: str, summary_id_text: str) -> None:
        group = STORE.get_group(group_id)
        if not group:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Group not found"})
            return

        try:
            summary_id = int(summary_id_text)
        except (TypeError, ValueError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid summary id"})
            return

        summary = STORE.mark_summary_read(group_id, summary_id)
        if summary is None:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Summary not found"})
            return

        self._json(HTTPStatus.OK, {"ok": True, "group": group, "summary": summary})

    def _summarize_group(self, group_id: str) -> None:
        body = self._read_json(default={})
        try:
            limit = parse_manual_summary_limit(body.get("limit"))
            mode = parse_manual_summary_mode(body.get("mode"))
        except ValueError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        mark_read = bool(body.get("mark_read", True))

        if not STORE.get_group(group_id):
            self._json(HTTPStatus.NOT_FOUND, {"error": "Group not found"})
            return
        if STORE.count_unread_message_records(group_id) <= 0:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "No unread messages to summarize"})
            return

        task, created = STORE.create_summary_task(
            task_id=secrets.token_urlsafe(18),
            group_id=group_id,
            requested_limit=limit,
            mark_read=mark_read,
            created_at=int(time.time()),
            pipeline_version=SUMMARY_PIPELINE_VERSION,
            mode=mode,
        )
        if created:
            STORE.add_summary_task_event(
                str(task["task_id"]),
                level="info",
                stage="queued",
                message="总结任务已进入后台队列。",
                created_at=int(time.time()),
            )
            MANUAL_SUMMARY_QUEUE.put(str(task["task_id"]))
            task = STORE.get_summary_task(str(task["task_id"])) or task

        self._json(
            HTTPStatus.ACCEPTED,
            {
                "ok": True,
                "created": created,
                "task": summary_task_payload(
                    task,
                    STORE.list_summary_task_events(str(task["task_id"])),
                ),
            },
        )

    def _handle_onebot_webhook(self, query: dict[str, list[str]]) -> None:
        token = (query.get("token") or [""])[0] or self.headers.get("X-QQ-Summary-Token")
        if not webhook_token_matches(token):
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Invalid webhook token"})
            return

        event = self._read_json(default={})
        if SETTINGS.webhook_debug:
            self._log_webhook_event(event)
        message = parse_group_message(event, reply_lookup=STORE.get_message)
        if message is None:
            self._json(HTTPStatus.OK, {"ok": True, "ignored": True})
            return

        group_name = group_name_from_event(event, message.group_id)
        STORE.upsert_group(message.group_id, group_name, message.timestamp)
        inserted = STORE.save_message(message, event)
        auto_summary_enqueued = False
        if inserted:
            if VOICE_ARCHIVER.enabled:
                register_voice_media(
                    {
                        "message_id": message.message_id,
                        "group_id": message.group_id,
                        "timestamp": message.timestamp,
                    },
                    event,
                    voice_archiver=VOICE_ARCHIVER,
                )
            auto_summary_enqueued = enqueue_auto_summary_if_needed(message.group_id)
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "inserted": inserted,
                "auto_summary_enqueued": auto_summary_enqueued,
                "message_id": message.message_id,
                "group_id": message.group_id,
            },
        )

    def _read_body(self) -> bytes:
        transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()
        if "chunked" in transfer_encoding:
            chunks: list[bytes] = []
            while True:
                line = self.rfile.readline()
                if not line:
                    break
                chunk_size_text = line.split(b";", 1)[0].strip()
                if not chunk_size_text:
                    continue
                chunk_size = int(chunk_size_text, 16)
                if chunk_size == 0:
                    while True:
                        trailer = self.rfile.readline()
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    break
                chunks.append(self.rfile.read(chunk_size))
                self.rfile.read(2)
            return b"".join(chunks)

        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _read_json(self, default: dict | None = None) -> dict:
        body = self._read_body()
        if not body:
            return default or {}
        raw = body.decode("utf-8")
        if not raw.strip():
            return default or {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return default or {}
        return data if isinstance(data, dict) else (default or {})

    def _log_webhook_event(self, event: dict) -> None:
        WEBHOOK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "received_at": int(time.time()),
            "post_type": event.get("post_type"),
            "message_type": event.get("message_type"),
            "sub_type": event.get("sub_type"),
            "group_id": event.get("group_id"),
            "user_id": event.get("user_id"),
            "message_id": event.get("message_id"),
            "keys": sorted(event.keys()),
            "raw": event,
        }
        with WEBHOOK_LOG_PATH.open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _serve_static(self, relative_path: str) -> None:
        safe_path = Path(relative_path).name if "/" not in relative_path else relative_path
        static_root = STATIC_DIR.resolve()
        file_path = (static_root / safe_path).resolve()
        try:
            file_path.relative_to(static_root)
        except ValueError:
            self._json(HTTPStatus.FORBIDDEN, {"error": "Forbidden"})
            return
        if not file_path.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        content_type = STATIC_CONTENT_TYPES.get(file_path.suffix.lower())
        if content_type is None:
            content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        content = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _json(self, status: HTTPStatus, payload: dict, cookies: list[str] | None = None) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        for cookie in cookies or []:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(content)


def run() -> None:
    STORE.init()
    recoverable_voice_count = VOICE_ARCHIVER.start()
    resumable_task_ids = STORE.requeue_interrupted_summary_tasks(SUMMARY_PIPELINE_VERSION)
    start_manual_summary_worker()
    for task_id in resumable_task_ids:
        MANUAL_SUMMARY_QUEUE.put(task_id)
    start_auto_summary_worker()
    refresh_count = refresh_stored_cq_messages()
    auto_summary_count = enqueue_existing_auto_summaries()
    address = (SETTINGS.host, SETTINGS.port)
    httpd = ThreadingHTTPServer(address, RequestHandler)
    print(f"QQ Message Summary running at http://{SETTINGS.host}:{SETTINGS.port}")
    print(f"SQLite database: {SETTINGS.database_path}")
    print(f"DeepSeek model: {SETTINGS.deepseek_model}")
    print(f"DeepSeek max concurrency: {SETTINGS.deepseek_max_concurrency}")
    print(
        "Auto summary: "
        f"{'enabled' if SETTINGS.auto_summary_enabled else 'disabled'} "
        f"(threshold {SETTINGS.auto_summary_threshold})"
    )
    print(
        "Special member summary: "
        f"{'configured' if SETTINGS.special_member_user_id else 'disabled (missing QQ user_id)'}"
    )
    print(
        "Voice archive: "
        f"{'enabled' if SETTINGS.voice_archive_enabled else 'disabled'} "
        f"(source {SETTINGS.voice_source_root}, media {SETTINGS.voice_media_path})"
    )
    print(f"Static frontend: {STATIC_DIR} ({'built' if (STATIC_DIR / 'index.html').is_file() else 'missing build'})")
    if refresh_count:
        print(f"Refreshed {refresh_count} stored CQ messages")
    if auto_summary_count:
        print(f"Queued {auto_summary_count} group(s) for automatic summary")
    if resumable_task_ids:
        print(f"Resumed {len(resumable_task_ids)} manual summary task(s)")
    if recoverable_voice_count:
        print(f"Resumed {recoverable_voice_count} voice archive task(s)")
    httpd.serve_forever()


def refresh_stored_cq_messages(limit: int = 5000) -> int:
    refreshed = 0
    for row in STORE.list_messages_with_raw_cq(limit=limit):
        try:
            raw = json.loads(row.get("raw_json") or "{}")
        except json.JSONDecodeError:
            continue
        content = message_to_text(raw.get("raw_message"), raw.get("message"), STORE.get_message)
        if content and content != row.get("content"):
            STORE.update_message_content(str(row["message_id"]), content)
            refreshed += 1
    return refreshed


if __name__ == "__main__":
    run()
