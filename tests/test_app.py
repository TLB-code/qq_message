import unittest
import threading
import time
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.onebot import (
    message_display_parts,
    message_to_summary_text,
    parse_group_message,
    special_member_relations,
)
from app.server import (
    AUTO_SUMMARY_BATCH_LIMIT,
    DEFAULT_SUMMARY_LIMIT,
    MEDIA_ONLY_SUMMARY,
    MAX_SUMMARY_LIMIT,
    create_web_session,
    enqueue_auto_summary_if_needed,
    is_valid_web_session,
    is_allowed_media_url,
    media_proxy_url,
    message_payload_from_record,
    parse_manual_summary_limit,
    process_manual_summary_task,
    query_bool,
    revoke_web_session,
    summarize_group_messages,
    summary_message_from_record,
    webhook_token_matches,
)
from app.storage import Message, Store
from app.summarizer import (
    DeepSeekClient,
    SummarizerError,
    build_summary_prompt,
    compact_messages,
    parse_summary_json,
    prepare_messages,
    render_summary,
    split_messages,
)


EMPTY_SUMMARY_JSON = json.dumps(
    {"items": []}
)


class AppTests(unittest.TestCase):
    def test_parse_group_message_from_onebot_event(self):
        event = {
            "post_type": "message",
            "message_type": "group",
            "time": 1718000001,
            "message_id": 2002,
            "group_id": 987654321,
            "user_id": 345678901,
            "raw_message": "大家好",
            "sender": {"nickname": "群友A", "card": "群名片"},
        }

        message = parse_group_message(event)

        self.assertIsNotNone(message)
        self.assertEqual(message.message_id, "2002")
        self.assertEqual(message.group_id, "987654321")
        self.assertEqual(message.sender_name, "群名片")
        self.assertEqual(message.content, "大家好")

    def test_parse_group_message_expands_raw_cq_reply(self):
        replied = Message("1358068059", "1", "u1", "origin", "quoted text", 1718000000)
        event = {
            "post_type": "message",
            "message_type": "group",
            "time": 1718000001,
            "message_id": 2003,
            "group_id": 1,
            "user_id": 2,
            "raw_message": "[CQ:reply,id=1358068059][CQ:at,qq=289770220] direct hit",
            "sender": {"nickname": "sender"},
        }

        message = parse_group_message(event, reply_lookup=lambda message_id: replied if message_id == "1358068059" else None)

        self.assertIsNotNone(message)
        self.assertNotIn("[CQ:", message.content)
        self.assertIn("origin", message.content)
        self.assertIn("quoted text", message.content)
        self.assertIn("@289770220", message.content)
        self.assertIn("direct hit", message.content)

    def test_parse_group_message_expands_segmented_reply(self):
        replied = Message("1358068059", "1", "u1", "origin", "quoted text", 1718000000)
        event = {
            "post_type": "message",
            "message_type": "group",
            "time": 1718000001,
            "message_id": 2003,
            "group_id": 1,
            "user_id": 2,
            "message": [
                {"type": "reply", "data": {"id": "1358068059"}},
                {"type": "at", "data": {"qq": "289770220"}},
                {"type": "text", "data": {"text": " direct hit"}},
            ],
            "sender": {"nickname": "sender"},
        }

        message = parse_group_message(event, reply_lookup=lambda message_id: replied if message_id == "1358068059" else None)

        self.assertIsNotNone(message)
        self.assertNotIn("[CQ:", message.content)
        self.assertIn("origin", message.content)
        self.assertIn("quoted text", message.content)
        self.assertIn("@289770220", message.content)
        self.assertIn("direct hit", message.content)

    def test_message_display_parts_show_image_and_face(self):
        raw_message = "[CQ:image,file=pic.jpg,url=https://example.com/pic.jpg][CQ:face,id=14] hello"

        parts = message_display_parts(raw_message, None)

        self.assertEqual(parts[0]["type"], "image")
        self.assertEqual(parts[0]["url"], "https://example.com/pic.jpg")
        self.assertEqual(parts[1]["type"], "face")
        self.assertEqual(parts[1]["name"], "14")
        self.assertEqual(parts[2]["type"], "text")
        self.assertIn("hello", parts[2]["text"])

    def test_message_display_parts_show_sticker_preview_when_url_exists(self):
        message = [
            {
                "type": "mface",
                "data": {
                    "summary": "funny sticker",
                    "url": "https://example.com/sticker.webp",
                },
            }
        ]

        parts = message_display_parts("", message)

        self.assertEqual(parts, [
            {
                "type": "sticker",
                "label": "表情包",
                "url": "https://example.com/sticker.webp",
                "name": "funny sticker",
            }
        ])

    def test_summary_text_excludes_image_and_face(self):
        raw_message = "before [CQ:image,file=pic.jpg,url=https://example.com/pic.jpg][CQ:face,id=14] after"

        text = message_to_summary_text(raw_message, None)

        self.assertEqual(text, "before after")
        self.assertNotIn("图片", text)
        self.assertNotIn("表情", text)

    def test_summary_message_from_record_uses_text_only_content(self):
        record = {
            "message_id": "media-1",
            "group_id": "1",
            "user_id": "u1",
            "sender_name": "sender",
            "content": "[图片:pic.jpg] hello [表情]",
            "timestamp": 1718000001,
            "raw_json": (
                '{"raw_message":"[CQ:image,file=pic.jpg] hello [CQ:face,id=14]",'
                '"message":null}'
            ),
        }

        message = summary_message_from_record(record)

        self.assertIsNotNone(message)
        self.assertEqual(message.content, "hello")

    def test_summary_message_from_media_only_record_is_skipped(self):
        record = {
            "message_id": "media-2",
            "group_id": "1",
            "user_id": "u1",
            "sender_name": "sender",
            "content": "[图片:pic.jpg][表情]",
            "timestamp": 1718000001,
            "raw_json": (
                '{"raw_message":"[CQ:image,file=pic.jpg][CQ:face,id=14]",'
                '"message":null}'
            ),
        }

        self.assertIsNone(summary_message_from_record(record))

    def test_message_payload_adds_media_proxy_url(self):
        record = {
            "message_id": "media-3",
            "group_id": "1",
            "user_id": "u1",
            "sender_name": "sender",
            "content": "[图片:pic.jpg]",
            "timestamp": 1718000001,
            "raw_json": (
                '{"raw_message":"[CQ:image,file=pic.jpg,url=https://example.com/pic.jpg]",'
                '"message":null}'
            ),
        }

        payload = message_payload_from_record(record)

        self.assertEqual(payload["display_parts"][0]["type"], "image")
        self.assertEqual(payload["display_parts"][0]["proxy_url"], media_proxy_url("https://example.com/pic.jpg"))

    def test_media_proxy_rejects_local_urls(self):
        self.assertTrue(is_allowed_media_url("https://multimedia.nt.qq.com.cn/download?fileid=1"))
        self.assertFalse(is_allowed_media_url("http://127.0.0.1:8000/private.png"))
        self.assertFalse(is_allowed_media_url("http://localhost/private.png"))

    def test_webhook_token_matching(self):
        import app.server as server

        original_token = server.SETTINGS.webhook_token
        try:
            object.__setattr__(server.SETTINGS, "webhook_token", None)
            self.assertTrue(webhook_token_matches(None))

            object.__setattr__(server.SETTINGS, "webhook_token", "secret-token")
            self.assertTrue(webhook_token_matches("secret-token"))
            self.assertFalse(webhook_token_matches("wrong-token"))
            self.assertFalse(webhook_token_matches(None))
        finally:
            object.__setattr__(server.SETTINGS, "webhook_token", original_token)

    def test_web_session_can_be_revoked(self):
        token = create_web_session()

        self.assertTrue(is_valid_web_session(token))

        revoke_web_session(token)

        self.assertFalse(is_valid_web_session(token))

    def test_query_bool_defaults_and_parses_flags(self):
        self.assertTrue(query_bool({}, "include_total", default=True))
        self.assertFalse(query_bool({"include_total": ["false"]}, "include_total", default=True))
        self.assertTrue(query_bool({"include_total": ["1"]}, "include_total"))

    def test_store_tracks_unread_cursor(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.sqlite3")
            store.init()
            store.upsert_group("1", "测试群", 100)
            store.save_message(Message("1", "1", "u1", "张三", "第一条", 100), {"message_id": 1})
            store.save_message(Message("2", "1", "u2", "李四", "第二条", 101), {"message_id": 2})

            unread = store.get_unread_messages("1")
            self.assertEqual([message.message_id for message in unread], ["1", "2"])

            store.save_summary("1", unread[:1], "第一条总结", "test-model", 102)
            unread_after_summary = store.get_unread_messages("1")
            self.assertEqual([message.message_id for message in unread_after_summary], ["2"])

    def test_store_counts_unread_records(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.sqlite3")
            store.init()
            store.upsert_group("1", "test group", 100)
            for index in range(4):
                store.save_message(
                    Message(str(index), "1", "u1", "user", f"message {index}", 100 + index),
                    {"message_id": index},
                )

            self.assertEqual(store.count_unread_message_records("1"), 4)

            first_two = store.get_unread_messages("1", limit=2)
            store.save_summary("1", first_two, "summary", "test-model", 200)

            self.assertEqual(store.count_unread_message_records("1"), 2)

    def test_store_tracks_async_summary_task_lifecycle(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.sqlite3")
            store.init()
            store.upsert_group("1", "test group", 100)

            task, created = store.create_summary_task("task-1", "1", 1000, True, 101)
            duplicate, duplicate_created = store.create_summary_task("task-2", "1", 500, True, 102)

            self.assertTrue(created)
            self.assertEqual(task["status"], "queued")
            self.assertFalse(duplicate_created)
            self.assertEqual(duplicate["task_id"], "task-1")
            self.assertEqual(store.get_active_summary_task("1")["task_id"], "task-1")

            store.mark_summary_task_running("task-1", 103)
            store.complete_summary_task("task-1", summary_id=7, message_count=321, completed_at=104)
            completed = store.get_summary_task("task-1", group_id="1")

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(completed["summary_id"], 7)
            self.assertEqual(completed["message_count"], 321)
            self.assertIsNone(store.get_active_summary_task("1"))

    def test_store_requeues_interrupted_summary_tasks_with_checkpoints(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.sqlite3")
            store.init()
            store.upsert_group("1", "test group", 100)
            store.create_summary_task("task-1", "1", 1000, True, 101)
            store.mark_summary_task_running("task-1", 102)
            store.set_summary_task_plan("task-1", 3)
            store.save_summary_task_chunk(
                "task-1",
                chunk_index=1,
                title="chunk 1",
                message_count=2,
                time_range="time",
                summary="saved chunk",
                completed_at=102,
            )

            task_ids = store.requeue_interrupted_summary_tasks()
            task = store.get_summary_task("task-1")

            self.assertEqual(task_ids, ["task-1"])
            self.assertEqual(task["status"], "queued")
            self.assertEqual(task["stage"], "resuming")
            self.assertEqual(task["completed_chunks"], 1)
            self.assertEqual(len(store.list_summary_task_chunks("task-1")), 1)

    def test_manual_summary_task_completes_in_background_processor(self):
        import app.server as server

        with TemporaryDirectory() as tmp:
            original_store = server.STORE
            store = Store(Path(tmp) / "test.sqlite3")
            store.init()
            store.upsert_group("1", "test group", 100)
            store.save_message(
                Message("1", "1", "u1", "user", "[图片:pic.jpg]", 100),
                {
                    "message_id": "1",
                    "raw_message": "[CQ:image,file=pic.jpg]",
                    "message": None,
                },
            )
            store.create_summary_task("task-1", "1", 1000, True, 101)

            try:
                server.STORE = store
                task = process_manual_summary_task("task-1")
            finally:
                server.STORE = original_store

            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["message_count"], 1)
            self.assertIsNotNone(task["summary_id"])
            self.assertEqual(store.count_unread_message_records("1"), 0)

    def test_manual_summary_task_resumes_without_repeating_saved_chunks(self):
        import app.server as server

        class ResumeFakeClient(DeepSeekClient):
            def __init__(self):
                super().__init__(
                    api_key="test",
                    chunk_max_messages=2,
                    chunk_max_chars=1000,
                    merge_max_blocks=10,
                    merge_max_chars=10000,
                    chunk_parallelism=2,
                )
                self.chunk_calls = []

            def _chat(self, messages):
                content = messages[-1]["content"]
                if "合并为" in content:
                    return EMPTY_SUMMARY_JSON
                self.chunk_calls.append(content)
                return EMPTY_SUMMARY_JSON

        with TemporaryDirectory() as tmp:
            original_store = server.STORE
            original_summarizer = server.SUMMARIZER
            store = Store(Path(tmp) / "test.sqlite3")
            store.init()
            store.upsert_group("1", "test group", 100)
            records = []
            for index in range(5):
                message = Message(str(index), "1", "u1", "user", f"message {index}", 100 + index)
                store.save_message(message, {"message_id": index})
            records = store.get_unread_message_records("1", limit=5)
            store.create_summary_task("task-1", "1", 5000, True, 105)
            store.mark_summary_task_running("task-1", 106)
            store.save_summary_task_message_snapshot("task-1", records)
            store.set_summary_task_plan("task-1", 3)
            store.save_summary_task_chunk(
                "task-1",
                chunk_index=1,
                title="第 1/3 块",
                message_count=2,
                time_range="06-10 00:00 - 06-10 00:01",
                summary=EMPTY_SUMMARY_JSON,
                completed_at=107,
            )
            store.requeue_interrupted_summary_tasks()
            client = ResumeFakeClient()

            try:
                server.STORE = store
                server.SUMMARIZER = client
                task = process_manual_summary_task("task-1")
            finally:
                server.STORE = original_store
                server.SUMMARIZER = original_summarizer

            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["completed_chunks"], 3)
            self.assertEqual(len(client.chunk_calls), 2)
            self.assertFalse(any("第 1/3 块" in prompt for prompt in client.chunk_calls))
            self.assertIn("暂无明确重点话题", store.list_summaries("1", limit=1)[0]["summary"])

    def test_store_pages_latest_unread_records_before_cursor(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.sqlite3")
            store.init()
            store.upsert_group("1", "test group", 100)
            for index in range(1, 7):
                store.save_message(
                    Message(str(index), "1", "u1", "user", f"message {index}", 100 + index),
                    {"message_id": index},
                )

            latest = store.list_unread_message_page_records("1", limit=3)
            older = store.list_unread_message_page_records(
                "1",
                limit=3,
                before_timestamp=latest[0]["timestamp"],
                before_message_id=latest[0]["message_id"],
            )

            self.assertEqual([row["message_id"] for row in latest], ["4", "5", "6"])
            self.assertEqual([row["message_id"] for row in older], ["1", "2", "3"])

    def test_store_unread_page_excludes_summary_cursor_with_message_id_tie_breaker(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.sqlite3")
            store.init()
            store.upsert_group("1", "test group", 100)
            messages = [
                Message(message_id, "1", "u1", "user", message_id, 100)
                for message_id in ("01", "02", "03", "04")
            ]
            for message in messages:
                store.save_message(message, {"message_id": message.message_id})

            store.save_summary("1", messages[:2], "summary", "test-model", 101)

            page = store.list_unread_message_page_records("1", limit=10)
            unchanged_summary_order = store.get_unread_message_records("1", limit=10)

            self.assertEqual([row["message_id"] for row in page], ["03", "04"])
            self.assertEqual([row["message_id"] for row in unchanged_summary_order], ["03", "04"])

    def test_store_maintains_cached_group_counts(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.sqlite3")
            store.init()
            store.upsert_group("1", "test group", 100)
            first = Message("1", "1", "u1", "user", "first", 100)
            second = Message("2", "1", "u1", "user", "second", 101)

            self.assertTrue(store.save_message(first, {"message_id": "1"}))
            self.assertTrue(store.save_message(second, {"message_id": "2"}))
            self.assertFalse(store.save_message(second, {"message_id": "2"}))

            group = store.get_group("1")
            self.assertEqual(group["message_count"], 2)
            self.assertEqual(group["unread_count"], 2)
            self.assertEqual(group["latest_timestamp"], 101)

            store.save_summary("1", [first], "summary", "test-model", 102)
            group_after_summary = store.get_group("1")
            self.assertEqual(group_after_summary["message_count"], 2)
            self.assertEqual(group_after_summary["unread_count"], 1)

            store.mark_read("1", now=103)
            self.assertEqual(store.get_group("1")["unread_count"], 0)

            store.save_message(Message("old", "1", "u1", "user", "old", 99), {"message_id": "old"})
            self.assertEqual(store.get_group("1")["unread_count"], 0)

            store.save_message(Message("3", "1", "u1", "user", "third", 102), {"message_id": "3"})
            final_group = store.get_group("1")
            self.assertEqual(final_group["message_count"], 4)
            self.assertEqual(final_group["unread_count"], 1)
            self.assertEqual(final_group["latest_timestamp"], 102)

    def test_store_migrates_and_backfills_group_stats(self):
        with TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "test.sqlite3"
            store = Store(database_path)
            with store.connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE groups (
                        group_id TEXT PRIMARY KEY,
                        group_name TEXT NOT NULL,
                        auto_summary_enabled INTEGER NOT NULL DEFAULT 0,
                        updated_at INTEGER NOT NULL
                    );
                    CREATE TABLE messages (
                        message_id TEXT PRIMARY KEY,
                        group_id TEXT NOT NULL,
                        user_id TEXT NOT NULL,
                        sender_name TEXT NOT NULL,
                        content TEXT NOT NULL,
                        timestamp INTEGER NOT NULL,
                        raw_json TEXT NOT NULL
                    );
                    CREATE TABLE summary_cursors (
                        group_id TEXT PRIMARY KEY,
                        last_message_id TEXT,
                        last_timestamp INTEGER,
                        updated_at INTEGER NOT NULL
                    );
                    """
                )
                conn.execute(
                    "INSERT INTO groups (group_id, group_name, updated_at) VALUES ('1', 'group', 102)"
                )
                conn.executemany(
                    """
                    INSERT INTO messages
                        (message_id, group_id, user_id, sender_name, content, timestamp, raw_json)
                    VALUES (?, '1', 'u1', 'user', ?, ?, '{}')
                    """,
                    [
                        ("1", "first", 100),
                        ("2", "second", 101),
                        ("3", "third", 102),
                    ],
                )
                conn.execute(
                    """
                    INSERT INTO summary_cursors
                        (group_id, last_message_id, last_timestamp, updated_at)
                    VALUES ('1', '1', 100, 103)
                    """
                )

            store.init()

            group = store.get_group("1")
            self.assertEqual(group["message_count"], 3)
            self.assertEqual(group["unread_count"], 2)
            self.assertEqual(group["latest_timestamp"], 102)
            self.assertEqual(store.list_groups()[0]["message_count"], 3)

    def test_store_migrates_existing_summary_task_progress_columns(self):
        with TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "test.sqlite3"
            store = Store(database_path)
            with store.connect() as conn:
                conn.executescript(
                    """
                    CREATE TABLE summary_tasks (
                        task_id TEXT PRIMARY KEY,
                        group_id TEXT NOT NULL,
                        requested_limit INTEGER NOT NULL,
                        mark_read INTEGER NOT NULL DEFAULT 1,
                        status TEXT NOT NULL,
                        summary_id INTEGER,
                        message_count INTEGER,
                        error TEXT,
                        created_at INTEGER NOT NULL,
                        started_at INTEGER,
                        completed_at INTEGER
                    );
                    """
                )

            store.init()

            with store.connect() as conn:
                columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(summary_tasks)").fetchall()
                }
            self.assertTrue({"stage", "total_messages", "total_chunks", "completed_chunks"} <= columns)

    def test_store_group_auto_summary_defaults_off_and_can_toggle(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.sqlite3")
            store.init()
            store.upsert_group("1", "test group", 100)

            group = store.get_group("1")
            self.assertEqual(group["auto_summary_enabled"], 0)
            self.assertFalse(store.is_group_auto_summary_enabled("1"))

            updated = store.set_group_auto_summary_enabled("1", True)
            self.assertEqual(updated["auto_summary_enabled"], 1)
            self.assertTrue(store.is_group_auto_summary_enabled("1"))

    def test_summarize_group_messages_saves_media_only_summary(self):
        with TemporaryDirectory() as tmp:
            import app.server as server

            original_store = server.STORE
            store = Store(Path(tmp) / "test.sqlite3")
            store.init()
            store.upsert_group("1", "test group", 100)
            store.save_message(
                Message("1", "1", "u1", "user", "[图片:pic.jpg]", 100),
                {
                    "message_id": "1",
                    "raw_message": "[CQ:image,file=pic.jpg]",
                    "message": None,
                },
            )

            try:
                server.STORE = store
                result = summarize_group_messages("1", limit=500)
            finally:
                server.STORE = original_store

            self.assertEqual(result["summary"], MEDIA_ONLY_SUMMARY)
            self.assertEqual(result["message_count"], 1)
            self.assertEqual(store.count_unread_message_records("1"), 0)

    def test_auto_summary_enqueue_requires_threshold(self):
        with TemporaryDirectory() as tmp:
            import app.server as server

            original_store = server.STORE
            original_threshold = server.SETTINGS.auto_summary_threshold
            original_enabled = server.SETTINGS.auto_summary_enabled
            store = Store(Path(tmp) / "test.sqlite3")
            store.init()
            store.upsert_group("1", "test group", 100)
            for index in range(3):
                store.save_message(
                    Message(str(index), "1", "u1", "user", f"message {index}", 100 + index),
                    {"message_id": index},
                )

            try:
                server.STORE = store
                object.__setattr__(server.SETTINGS, "auto_summary_enabled", True)
                object.__setattr__(server.SETTINGS, "auto_summary_threshold", 3)
                server.AUTO_SUMMARY_QUEUED_GROUPS.clear()
                while not server.AUTO_SUMMARY_QUEUE.empty():
                    server.AUTO_SUMMARY_QUEUE.get_nowait()
                    server.AUTO_SUMMARY_QUEUE.task_done()

                self.assertFalse(enqueue_auto_summary_if_needed("1"))
                store.set_group_auto_summary_enabled("1", True)
                self.assertTrue(enqueue_auto_summary_if_needed("1"))
                self.assertFalse(enqueue_auto_summary_if_needed("1"))
            finally:
                server.AUTO_SUMMARY_QUEUED_GROUPS.clear()
                while not server.AUTO_SUMMARY_QUEUE.empty():
                    server.AUTO_SUMMARY_QUEUE.get_nowait()
                    server.AUTO_SUMMARY_QUEUE.task_done()
                server.STORE = original_store
                object.__setattr__(server.SETTINGS, "auto_summary_enabled", original_enabled)
                object.__setattr__(server.SETTINGS, "auto_summary_threshold", original_threshold)

    def test_store_pages_history_before_cursor(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.sqlite3")
            store.init()
            store.upsert_group("1", "test group", 100)
            for index in range(1, 6):
                store.save_message(
                    Message(str(index), "1", f"u{index}", f"user{index}", f"message {index}", 100 + index),
                    {"message_id": index},
                )

            latest = store.list_history_message_records("1", limit=2)
            older = store.list_history_message_records(
                "1",
                limit=2,
                before_timestamp=latest[0]["timestamp"],
                before_message_id=latest[0]["message_id"],
            )

            self.assertEqual([row["message_id"] for row in latest], ["4", "5"])
            self.assertEqual([row["message_id"] for row in older], ["2", "3"])

    def test_store_filters_and_deletes_history_by_day_range(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.sqlite3")
            store.init()
            store.upsert_group("1", "test group", 100)
            store.save_message(Message("old", "1", "u1", "user", "old", 99), {"message_id": "old"})
            store.save_message(Message("in-1", "1", "u1", "user", "in 1", 100), {"message_id": "in-1"})
            store.save_message(Message("in-2", "1", "u1", "user", "in 2", 101), {"message_id": "in-2"})
            store.save_message(Message("new", "1", "u1", "user", "new", 200), {"message_id": "new"})
            store.save_summary(
                "1",
                [Message("old", "1", "u1", "user", "old", 99)],
                "old summary",
                "test-model",
                201,
            )

            records = store.list_history_message_records(
                "1",
                limit=10,
                start_timestamp=100,
                end_timestamp=200,
            )
            total_count = store.count_message_records("1")
            range_count = store.count_message_records("1", start_timestamp=100, end_timestamp=200)
            deleted_count = store.delete_message_records_in_range("1", 100, 200)
            remaining = store.list_history_message_records("1", limit=10)

            self.assertEqual([row["message_id"] for row in records], ["in-1", "in-2"])
            self.assertEqual(total_count, 4)
            self.assertEqual(range_count, 2)
            self.assertEqual(deleted_count, 2)
            self.assertEqual([row["message_id"] for row in remaining], ["old", "new"])
            group = store.get_group("1")
            self.assertEqual(group["message_count"], 2)
            self.assertEqual(group["unread_count"], 1)
            self.assertEqual(group["latest_timestamp"], 200)

    def test_store_pages_summaries_before_id(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.sqlite3")
            store.init()
            store.upsert_group("1", "test group", 100)
            for index in range(7):
                message = Message(str(index), "1", "u1", "user", f"message {index}", 100 + index)
                store.save_message(message, {"message_id": index})
                store.save_summary(
                    "1",
                    [message],
                    f"summary {index}",
                    "test-model",
                    200 + index,
                    mark_read=False,
                )

            first_page = store.list_summaries("1", limit=5)
            second_page = store.list_summaries("1", limit=5, before_id=first_page[-1]["id"])

            self.assertEqual(store.count_summaries("1"), 7)
            self.assertEqual([row["summary"] for row in first_page], [
                "summary 6",
                "summary 5",
                "summary 4",
                "summary 3",
                "summary 2",
            ])
            self.assertEqual([row["summary"] for row in second_page], ["summary 1", "summary 0"])

    def test_store_marks_summary_read(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.sqlite3")
            store.init()
            store.upsert_group("1", "test group", 100)
            message = Message("1", "1", "u1", "user", "message", 100)
            store.save_message(message, {"message_id": "1"})
            summary_id = store.save_summary("1", [message], "summary", "test-model", 101, mark_read=False)

            before = store.list_summaries("1", limit=1)[0]
            updated = store.mark_summary_read("1", summary_id)
            after = store.list_summaries("1", limit=1)[0]

            self.assertEqual(before["is_read"], 0)
            self.assertEqual(updated["is_read"], 1)
            self.assertEqual(after["is_read"], 1)

    def test_store_migrates_summary_read_column(self):
        with TemporaryDirectory() as tmp:
            database_path = Path(tmp) / "test.sqlite3"
            store = Store(database_path)
            with store.connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE summaries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id TEXT NOT NULL,
                        from_message_id TEXT,
                        to_message_id TEXT,
                        from_timestamp INTEGER,
                        to_timestamp INTEGER,
                        message_count INTEGER NOT NULL,
                        model TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        created_at INTEGER NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO summaries (
                        group_id,
                        from_message_id,
                        to_message_id,
                        from_timestamp,
                        to_timestamp,
                        message_count,
                        model,
                        summary,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    ("1", "1", "1", 100, 100, 1, "test-model", "summary", 101),
                )

            store.init()
            row = store.list_summaries("1", limit=1)[0]

            self.assertEqual(row["is_read"], 0)

    def test_compact_messages_keeps_content_under_limit(self):
        messages = [
            Message(str(i), "1", "u", "群友", f"消息 {i} " + "x" * 50, 1718000000 + i)
            for i in range(400)
        ]

        compacted = compact_messages(messages, max_chars=2000)

        self.assertLessEqual(len(compacted), 2200)
        self.assertIn("中间省略", compacted)

    def test_summary_prompt_has_special_member_identity_rules(self):
        messages = [Message("1", "1", "u", "魔女公主♪", "hello", 1718000000)]

        prompt = build_summary_prompt("test group", messages)

        self.assertIn('"items"', prompt[-1]["content"])
        self.assertIn("重点成员本人", prompt[-1]["content"])
        self.assertIn("魔女公主♪", prompt[-1]["content"])

    def test_summary_limit_is_one_batch(self):
        self.assertEqual(DEFAULT_SUMMARY_LIMIT, 5000)
        self.assertEqual(MAX_SUMMARY_LIMIT, 5000)
        self.assertEqual(AUTO_SUMMARY_BATCH_LIMIT, 500)

    def test_manual_summary_limit_defaults_and_validates_range(self):
        self.assertEqual(parse_manual_summary_limit(None), 5000)
        self.assertEqual(parse_manual_summary_limit(""), 5000)
        self.assertEqual(parse_manual_summary_limit("1500"), 1500)
        with self.assertRaises(ValueError):
            parse_manual_summary_limit(0)
        with self.assertRaises(ValueError):
            parse_manual_summary_limit(5001)
        with self.assertRaises(ValueError):
            parse_manual_summary_limit("1.5")

    def test_split_messages_preserves_order(self):
        messages = [
            Message(str(i), "1", "u", "user", f"message {i}", 1718000000 + i)
            for i in range(5)
        ]

        chunks = split_messages(messages, max_messages=2, max_chars=1000)

        self.assertEqual([[message.message_id for message in chunk] for chunk in chunks], [["0", "1"], ["2", "3"], ["4"]])

    def test_deepseek_client_uses_chunked_summary_for_large_batches(self):
        class FakeDeepSeekClient(DeepSeekClient):
            def __init__(self):
                super().__init__(
                    api_key="test",
                    chunk_max_messages=2,
                    chunk_max_chars=1000,
                    merge_max_blocks=10,
                    merge_max_chars=10000,
                )
                self.calls = []

            def _chat(self, messages):
                self.calls.append(messages)
                return EMPTY_SUMMARY_JSON

        messages = [
            Message(str(i), "1", "u", "user", f"message {i}", 1718000000 + i)
            for i in range(5)
        ]
        client = FakeDeepSeekClient()

        summary = client.summarize("test group", messages)

        self.assertIn("暂无明确重点话题", summary)
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(client.calls[-1][0]["role"], "system")

    def test_deepseek_client_summarizes_chunks_in_parallel(self):
        class ParallelFakeClient(DeepSeekClient):
            def __init__(self):
                super().__init__(
                    api_key="test",
                    chunk_max_messages=1,
                    chunk_max_chars=1000,
                    merge_max_blocks=10,
                    merge_max_chars=10000,
                    chunk_parallelism=3,
                )
                self.lock = threading.Lock()
                self.active_chunk_calls = 0
                self.max_active_chunk_calls = 0

            def _chat(self, messages):
                content = messages[-1]["content"]
                if "合并为" in content:
                    return EMPTY_SUMMARY_JSON
                with self.lock:
                    self.active_chunk_calls += 1
                    self.max_active_chunk_calls = max(
                        self.max_active_chunk_calls,
                        self.active_chunk_calls,
                    )
                time.sleep(0.05)
                with self.lock:
                    self.active_chunk_calls -= 1
                return EMPTY_SUMMARY_JSON

        messages = [
            Message(str(i), "1", "u", "user", f"message {i}", 1718000000 + i)
            for i in range(3)
        ]
        client = ParallelFakeClient()

        summary = client.summarize("test group", messages)

        self.assertIn("暂无明确重点话题", summary)
        self.assertGreaterEqual(client.max_active_chunk_calls, 2)

    def test_summary_text_removes_nested_animated_sticker_placeholder(self):
        self.assertEqual(
            message_to_summary_text(None, None, fallback_content="[图片:[动画表情]]"),
            "",
        )
        self.assertEqual(
            message_to_summary_text(
                None,
                None,
                fallback_content="重要文字 [图片:[动画表情]]",
            ),
            "重要文字",
        )

    def test_special_member_relations_use_qq_user_id(self):
        replied = Message("10", "1", "special-qq", "已经改名", "原消息", 100)
        message = [
            {"type": "reply", "data": {"id": "10"}},
            {"type": "at", "data": {"qq": "special-qq"}},
            {"type": "text", "data": {"text": "请看"}},
        ]

        mentions, replies = special_member_relations(
            None,
            message,
            "special-qq",
            reply_lookup=lambda message_id: replied if message_id == "10" else None,
        )

        self.assertTrue(mentions)
        self.assertTrue(replies)

    def test_prepare_messages_uses_stable_alias_and_does_not_trust_nickname(self):
        messages = [
            Message("1", "1", "2829556413", "魔女公主♪", "本人", 100),
            Message("2", "1", "1709094324", "魔女公主♪（伪）", "？", 101),
            Message(
                "3",
                "1",
                "1709094324",
                "后来改名",
                "@魔女公主♪(2829556413) @123456789 请看",
                102,
            ),
        ]

        prepared = prepare_messages(messages, "2829556413")

        self.assertTrue(prepared[0].is_special_sender)
        self.assertEqual(prepared[0].sender_alias, "重点成员")
        self.assertFalse(prepared[1].is_special_sender)
        self.assertEqual(prepared[1].sender_alias, prepared[2].sender_alias)
        self.assertIn("@重点成员（魔女公主♪）", prepared[2].content)
        self.assertIn("@某成员", prepared[2].content)
        self.assertNotIn("2829556413", prepared[2].content)
        self.assertNotIn("123456789", prepared[2].content)

    def test_summary_json_requires_valid_and_special_evidence(self):
        payload = {
            "items": [
                {
                    "title": "安排",
                    "type": "confirmed_plan",
                    "action_state": "open",
                    "attention_reason": "none",
                    "claims": [{"text": "成员明确说明周五处理", "evidence": [2]}],
                },
                {
                    "title": "重点成员回复",
                    "type": "confirmed_fact",
                    "action_state": "none",
                    "attention_reason": "none",
                    "claims": [{"text": "重点成员进行了回复", "evidence": [1]}],
                },
            ],
        }

        parsed = parse_summary_json(json.dumps(payload), {1, 2}, {1})
        self.assertFalse(parsed["items"][0]["special_related"])
        self.assertTrue(parsed["items"][1]["special_related"])
        self.assertEqual(parsed["items"][1]["special_evidence"], [1])

        payload["items"][1]["claims"][0]["evidence"] = [3]
        with self.assertRaisesRegex(SummarizerError, "outside"):
            parse_summary_json(json.dumps(payload), {1, 2}, {1})

    def test_summary_json_accepts_wrapped_json_object(self):
        wrapped = (
            "以下是 JSON 结果：\n"
            '{"items":[]}\n'
            "处理完毕。"
        )

        parsed = parse_summary_json(wrapped, set(), set())

        self.assertEqual(parsed, {"items": []})

    def test_summary_json_accepts_hash_prefixed_evidence_positions(self):
        payload = {
            "items": [
                {
                    "title": "投票",
                    "type": "proposal",
                    "action_state": "open",
                    "attention_reason": "none",
                    "claims": [{"text": "正在收集名单", "evidence": ["#260"]}],
                }
            ]
        }

        parsed = parse_summary_json(json.dumps(payload), {260}, set())

        self.assertEqual(parsed["items"][0]["claims"][0]["evidence"], [260])

    def test_summary_json_rejects_banter_promoted_to_attention(self):
        payload = {
            "items": [
                {
                    "title": "象棋互怼",
                    "type": "banter",
                    "action_state": "none",
                    "attention_reason": "explicit_concern",
                    "claims": [{"text": "成员互相开玩笑", "evidence": [1]}],
                }
            ]
        }

        with self.assertRaisesRegex(SummarizerError, "Banter"):
            parse_summary_json(json.dumps(payload), {1}, set())

    def test_reported_concern_requires_explicit_attention_reason(self):
        payload = {
            "items": [
                {
                    "title": "普通转述",
                    "type": "reported_concern",
                    "action_state": "none",
                    "attention_reason": "none",
                    "claims": [{"text": "成员转述了一件事", "evidence": [1]}],
                }
            ]
        }

        with self.assertRaisesRegex(SummarizerError, "Reported concern"):
            parse_summary_json(json.dumps(payload), {1}, set())

    def test_structured_retry_repairs_mixed_self_report_and_concern(self):
        class ConcernRepairClient(DeepSeekClient):
            def __init__(self):
                super().__init__(api_key="test")
                self.calls = []

            def _chat(self, messages):
                self.calls.append(messages)
                if len(self.calls) == 1:
                    return json.dumps(
                        {
                            "items": [
                                {
                                    "title": "睡眠与群友担忧",
                                    "type": "self_report",
                                    "action_state": "none",
                                    "attention_reason": "explicit_concern",
                                    "claims": [
                                        {"text": "成员001说自己没睡多久", "evidence": [1]},
                                        {"text": "成员002明确劝其休息", "evidence": [2]},
                                    ],
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {
                        "items": [
                            {
                                "title": "睡眠自述",
                                "type": "self_report",
                                "action_state": "none",
                                "attention_reason": "none",
                                "claims": [
                                    {"text": "成员001说自己没睡多久", "evidence": [1]}
                                ],
                            },
                            {
                                "title": "群友明确劝休息",
                                "type": "reported_concern",
                                "action_state": "none",
                                "attention_reason": "explicit_concern",
                                "claims": [
                                    {"text": "成员002明确劝对方休息", "evidence": [2]}
                                ],
                            },
                        ]
                    },
                    ensure_ascii=False,
                )

        client = ConcernRepairClient()
        source_messages = prepare_messages(
            [
                Message("1", "1", "u1", "甲", "我没睡多久", 100),
                Message("2", "1", "u2", "乙", "你快去休息吧", 101),
            ],
            None,
        )

        payload = client._chat_structured(
            [{"role": "user", "content": "总结"}],
            source_messages,
        )

        self.assertEqual(len(payload["items"]), 2)
        self.assertEqual(payload["items"][0]["type"], "self_report")
        self.assertEqual(payload["items"][1]["type"], "reported_concern")
        self.assertEqual(client.calls[1][-2]["role"], "assistant")
        self.assertIn('"explicit_concern"', client.calls[1][-2]["content"])
        self.assertIn("必须按各自直接证据拆成", client.calls[1][-1]["content"])

    def test_confirmed_plan_is_programmatically_routed_as_open(self):
        payload = {
            "items": [
                {
                    "title": "周末补货",
                    "type": "confirmed_plan",
                    "action_state": "none",
                    "attention_reason": "none",
                    "claims": [{"text": "成员周末去补货", "evidence": [1]}],
                }
            ]
        }

        parsed = parse_summary_json(json.dumps(payload), {1}, set())

        self.assertEqual(parsed["items"][0]["action_state"], "open")

    def test_claim_cannot_reference_unrelated_member_alias(self):
        messages = prepare_messages(
            [
                Message(str(index), "1", f"u{index}", f"用户{index}", "消息", 100 + index)
                for index in range(1, 14)
            ],
            None,
        )
        payload = {
            "items": [
                {
                    "title": "身份测试",
                    "type": "self_report",
                    "action_state": "none",
                    "attention_reason": "none",
                    "special_related": False,
                    "claims": [{"text": "成员009作出说明", "evidence": [13]}],
                    "evidence": [13],
                }
            ]
        }

        with self.assertRaisesRegex(SummarizerError, "Claim aliases"):
            DeepSeekClient._validate_claim_aliases(payload, messages)

    def test_render_summary_derives_participants_and_ranks_overview(self):
        messages = prepare_messages(
            [
                Message(str(index), "1", f"u{index}", f"用户{index}", f"消息{index}", 100 + index * 60, position=index)
                for index in range(1, 9)
            ],
            None,
        )
        items = [
            {
                "title": f"短话题{index}",
                "type": "confirmed_fact",
                "action_state": "none",
                "attention_reason": "none",
                "special_related": False,
                "claims": [{"text": f"成员{index:03d}发言", "evidence": [index]}],
                "evidence": [index],
            }
            for index in range(1, 8)
        ]
        items.append(
            {
                "title": "持续投票",
                "type": "proposal",
                "action_state": "decided",
                "attention_reason": "none",
                "special_related": False,
                "claims": [{"text": "群内持续进行名单投票", "evidence": [1, 3, 5, 8]}],
                "evidence": [1, 3, 5, 8],
            }
        )

        summary = render_summary({"items": items}, messages, messages, 1, "魔女公主♪")
        overview = summary.split("## 重点话题", 1)[0]

        self.assertIn("持续投票", overview)
        self.assertIn("参与者：用户1、用户3、用户5、用户8", summary)
        self.assertNotIn("成员001（用户1）", summary)

    def test_deepseek_client_retries_read_timeouts(self):
        client = DeepSeekClient(api_key="test", timeout=30, request_retries=2)

        with (
            patch(
                "app.summarizer.urllib.request.urlopen",
                side_effect=TimeoutError("The read operation timed out"),
            ) as urlopen,
            patch("app.summarizer.time.sleep"),
        ):
            with self.assertRaisesRegex(SummarizerError, "after 3 attempts"):
                client._chat([{"role": "user", "content": "JSON"}])

        self.assertEqual(urlopen.call_count, 3)

    def test_render_summary_uses_program_computed_range_and_gap(self):
        messages = [
            Message("1", "1", "u1", "甲", "第一条", 100, position=1),
            Message("2", "1", "u2", "乙", "第二条", 788, position=2),
        ]
        payload = {"items": []}

        summary = render_summary(payload, messages, messages, 1, "魔女公主♪")

        self.assertIn("原始消息 2 条", summary)
        self.assertIn("最长间隔：11 分 28 秒", summary)

    def test_requeue_discards_incompatible_pipeline_checkpoints(self):
        with TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.sqlite3")
            store.init()
            store.upsert_group("1", "test group", 100)
            store.create_summary_task(
                "old-task",
                "1",
                5000,
                True,
                101,
                pipeline_version=1,
            )
            store.save_summary_task_chunk(
                "old-task",
                chunk_index=1,
                title="old",
                message_count=1,
                time_range="old",
                summary="old text format",
                completed_at=102,
            )

            task_ids = store.requeue_interrupted_summary_tasks(pipeline_version=3)
            task = store.get_summary_task("old-task")

            self.assertEqual(task_ids, ["old-task"])
            self.assertEqual(task["pipeline_version"], 3)
            self.assertEqual(task["completed_chunks"], 0)
            self.assertEqual(store.list_summary_task_chunks("old-task"), [])

    def test_merge_rejects_evidence_type_promotion(self):
        class PromotionClient(DeepSeekClient):
            def __init__(self):
                super().__init__(
                    api_key="test",
                    chunk_max_messages=1,
                    chunk_max_chars=1000,
                    merge_max_blocks=10,
                    merge_max_chars=10000,
                )

            def _chat(self, messages):
                content = messages[-1]["content"]
                if "合并为最终结构" in content or "上次输出未通过校验" in content:
                    item_type = "confirmed_fact"
                    evidence = 1
                else:
                    item_type = "banter"
                    evidence = 1 if "[#1]" in content else 2
                return json.dumps(
                    {
                        "items": [
                            {
                                "title": "线下玩笑",
                                "type": item_type,
                                "action_state": "none",
                                "attention_reason": "none",
                                "claims": [
                                    {"text": "成员001开玩笑提到线下", "evidence": [evidence]}
                                ],
                            }
                        ]
                    }
                )

        messages = [
            Message("1", "1", "u1", "甲", "我要线下了（笑）", 100),
            Message("2", "1", "u2", "乙", "玩笑回应", 101),
        ]

        with self.assertRaises(SummarizerError):
            PromotionClient().summarize("test group", messages)

    def test_merge_can_inherit_action_state_from_one_source_item(self):
        class ActionMergeClient(DeepSeekClient):
            def __init__(self):
                super().__init__(
                    api_key="test",
                    chunk_max_messages=1,
                    chunk_max_chars=1000,
                    merge_max_blocks=10,
                    merge_max_chars=10000,
                )

            def _chat(self, messages):
                content = messages[-1]["content"]
                if "当前待合并摘要数" in content or "候选事件：" in content:
                    evidence = [1, 2]
                    action_state = "decided"
                elif "第 1/2 块" in content:
                    evidence = [1]
                    action_state = "decided"
                elif "第 2/2 块" in content:
                    evidence = [2]
                    action_state = "none"
                else:
                    evidence = [1, 2]
                    action_state = "decided"
                return json.dumps(
                    {
                        "items": [
                            {
                                "title": "名单决定",
                                "type": "confirmed_fact",
                                "action_state": action_state,
                                "attention_reason": "none",
                                "claims": [{"text": "名单已经决定", "evidence": evidence}],
                            }
                        ]
                    }
                )

        messages = [
            Message("1", "1", "u1", "甲", "名单决定了", 100),
            Message("2", "1", "u2", "乙", "收到", 101),
        ]

        summary = ActionMergeClient().summarize("test group", messages)

        self.assertIn("已决定", summary)


if __name__ == "__main__":
    unittest.main()
