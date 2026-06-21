from __future__ import annotations

import json
import mimetypes
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import BASE_DIR, load_settings
from .onebot import group_name_from_event, parse_group_message
from .storage import Store
from .summarizer import DeepSeekClient, SummarizerError


SETTINGS = load_settings()
STORE = Store(SETTINGS.database_path)
SUMMARIZER = DeepSeekClient(
    api_key=SETTINGS.deepseek_api_key,
    base_url=SETTINGS.deepseek_base_url,
    model=SETTINGS.deepseek_model,
)
STATIC_DIR = BASE_DIR / "app" / "static"
WEBHOOK_LOG_PATH = SETTINGS.database_path.parent / "webhook_events.log"


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "QQSummary/0.1"

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._serve_static("index.html")
            return
        if parsed.path.startswith("/static/"):
            self._serve_static(parsed.path.removeprefix("/static/"))
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

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/webhook/onebot":
            self._handle_onebot_webhook()
            return
        if parsed.path.startswith("/api/groups/"):
            self._handle_group_post(parsed.path)
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _handle_group_get(self, path: str, query: dict[str, list[str]]) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[0] != "api" or parts[1] != "groups":
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

        group_id = parts[2]
        if group_id.endswith("/"):
            group_id = group_id[:-1]

        if path.endswith("/messages"):
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
                "unread": [message.__dict__ for message in STORE.get_unread_messages(group_id, limit=limit)],
                "recent": [message.__dict__ for message in STORE.list_recent_messages(group_id, limit=limit)],
                "summaries": STORE.list_summaries(group_id),
            },
        )

    def _handle_group_post(self, path: str) -> None:
        parts = path.strip("/").split("/")
        if len(parts) != 4 or parts[0] != "api" or parts[1] != "groups":
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

    def _summarize_group(self, group_id: str) -> None:
        group = STORE.get_group(group_id)
        if not group:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Group not found"})
            return

        body = self._read_json(default={})
        limit = int(body.get("limit", 500))
        mark_read = bool(body.get("mark_read", True))
        messages = STORE.get_unread_messages(group_id, limit=limit)
        if not messages:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "No unread messages to summarize"})
            return

        try:
            summary = SUMMARIZER.summarize(group["group_name"], messages)
        except SummarizerError as exc:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
            return

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
        message = parse_group_message(event)
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
        file_path = (STATIC_DIR / safe_path).resolve()
        if STATIC_DIR.resolve() not in file_path.parents and file_path != STATIC_DIR.resolve():
            self._json(HTTPStatus.FORBIDDEN, {"error": "Forbidden"})
            return
        if not file_path.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return

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
    address = (SETTINGS.host, SETTINGS.port)
    httpd = ThreadingHTTPServer(address, RequestHandler)
    print(f"QQ Message Summary running at http://{SETTINGS.host}:{SETTINGS.port}")
    print(f"SQLite database: {SETTINGS.database_path}")
    print(f"DeepSeek model: {SETTINGS.deepseek_model}")
    httpd.serve_forever()


if __name__ == "__main__":
    run()
