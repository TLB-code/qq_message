from __future__ import annotations

import ipaddress
import json
import mimetypes
import time
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

from .config import BASE_DIR, load_settings
from .onebot import (
    group_name_from_event,
    message_display_parts,
    message_to_summary_text,
    message_to_text,
    parse_group_message,
)
from .storage import Message, Store
from .summarizer import DeepSeekClient, SummarizerError


SETTINGS = load_settings()
STORE = Store(SETTINGS.database_path)
SUMMARIZER = DeepSeekClient(
    api_key=SETTINGS.deepseek_api_key,
    base_url=SETTINGS.deepseek_base_url,
    model=SETTINGS.deepseek_model,
)
STATIC_DIR = BASE_DIR / "frontend" / "dist"
WEBHOOK_LOG_PATH = SETTINGS.database_path.parent / "webhook_events.log"
DEFAULT_SUMMARY_LIMIT = 500
MAX_SUMMARY_LIMIT = 500
DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 100
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
    )


def message_payload_from_record(record: dict) -> dict:
    event = raw_event_from_record(record)
    payload = message_from_record(record).__dict__
    display_parts = message_display_parts(
        event.get("raw_message"),
        event.get("message"),
        fallback_content=payload["content"],
        reply_lookup=STORE.get_message,
    )
    for part in display_parts:
        media_url = part.get("url")
        if isinstance(media_url, str) and is_allowed_media_url(media_url):
            part["proxy_url"] = media_proxy_url(media_url)
    payload["display_parts"] = display_parts
    return payload


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
    return message_from_record(record, content=content)


def parse_history_date(value: str) -> tuple[int, int, str]:
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Invalid date, expected YYYY-MM-DD") from exc

    local_timezone = datetime.now().astimezone().tzinfo
    started_at = datetime.combine(day, datetime.min.time(), tzinfo=local_timezone)
    ended_at = started_at + timedelta(days=1)
    return int(started_at.timestamp()), int(ended_at.timestamp()), day.isoformat()


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "QQSummary/0.1"

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in ("", "/"):
            self._serve_static("index.html")
            return
        if parsed.path.startswith("/assets/"):
            self._serve_static(parsed.path.lstrip("/"))
            return
        if parsed.path.startswith("/static/"):
            self._serve_static(parsed.path.removeprefix("/static/"))
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

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/webhook/onebot":
            self._handle_onebot_webhook()
            return
        if parsed.path.startswith("/api/groups/"):
            self._handle_group_post(parsed.path)
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/groups/"):
            self._handle_group_delete(parsed.path, parse_qs(parsed.query))
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _handle_group_get(self, path: str, query: dict[str, list[str]]) -> None:
        parts = path.strip("/").split("/")
        if len(parts) not in (3, 4) or parts[0] != "api" or parts[1] != "groups":
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        group_id = parts[2]
        if group_id.endswith("/"):
            group_id = group_id[:-1]

        if len(parts) == 4:
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
                    message_payload_from_record(record)
                    for record in STORE.get_unread_message_records(group_id, limit=limit)
                ],
                "recent": [
                    message_payload_from_record(record)
                    for record in STORE.list_recent_message_records(group_id, limit=limit)
                ],
                "summaries": STORE.list_summaries(group_id),
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

        self._json(
            HTTPStatus.OK,
            {
                "group": group,
                "summaries": summaries,
                "has_more": has_more,
                "next_cursor": next_cursor,
                "limit": limit,
            },
        )

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
        messages = [message_payload_from_record(record) for record in records]
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
                "has_more": has_more,
                "next_cursor": next_cursor,
                "limit": limit,
                "date": selected_date,
            },
        )

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

        deleted_count = STORE.delete_message_records_in_range(group_id, start_timestamp, end_timestamp)
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "group": group,
                "date": selected_date,
                "deleted_count": deleted_count,
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
        if action == "mark-read":
            try:
                result = STORE.mark_read(group_id, now=int(time.time()))
            except ValueError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            self._json(HTTPStatus.OK, {"ok": True, **result})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

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
        group = STORE.get_group(group_id)
        if not group:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Group not found"})
            return

        body = self._read_json(default={})
        try:
            requested_limit = int(body.get("limit", DEFAULT_SUMMARY_LIMIT))
        except (TypeError, ValueError):
            requested_limit = DEFAULT_SUMMARY_LIMIT
        limit = min(max(requested_limit, 1), MAX_SUMMARY_LIMIT)
        mark_read = bool(body.get("mark_read", True))
        records = STORE.get_unread_message_records(group_id, limit=limit)
        messages = [message_from_record(record) for record in records]
        summary_messages = [
            message
            for message in (summary_message_from_record(record) for record in records)
            if message is not None
        ]
        if not messages:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "No unread messages to summarize"})
            return

        if summary_messages:
            try:
                summary = SUMMARIZER.summarize(group["group_name"], summary_messages)
            except SummarizerError as exc:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                return
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
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "summary_id": summary_id,
                "summary": summary,
                "message_count": len(messages),
                "model": SETTINGS.deepseek_model,
            },
        )

    def _handle_onebot_webhook(self) -> None:
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
        self._json(
            HTTPStatus.OK,
            {
                "ok": True,
                "inserted": inserted,
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

    def _json(self, status: HTTPStatus, payload: dict) -> None:
        content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def run() -> None:
    STORE.init()
    refresh_count = refresh_stored_cq_messages()
    address = (SETTINGS.host, SETTINGS.port)
    httpd = ThreadingHTTPServer(address, RequestHandler)
    print(f"QQ Message Summary running at http://{SETTINGS.host}:{SETTINGS.port}")
    print(f"SQLite database: {SETTINGS.database_path}")
    print(f"DeepSeek model: {SETTINGS.deepseek_model}")
    print(f"Static frontend: {STATIC_DIR} ({'built' if (STATIC_DIR / 'index.html').is_file() else 'missing build'})")
    if refresh_count:
        print(f"Refreshed {refresh_count} stored CQ messages")
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
