import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.onebot import message_display_parts, message_to_summary_text, parse_group_message
from app.server import (
    DEFAULT_SUMMARY_LIMIT,
    MEDIA_ONLY_SUMMARY,
    MAX_SUMMARY_LIMIT,
    create_web_session,
    enqueue_auto_summary_if_needed,
    is_valid_web_session,
    is_allowed_media_url,
    media_proxy_url,
    message_payload_from_record,
    query_bool,
    revoke_web_session,
    summarize_group_messages,
    summary_message_from_record,
    webhook_token_matches,
)
from app.storage import Message, Store
from app.summarizer import DeepSeekClient, build_summary_prompt, compact_messages, split_messages


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

    def test_summary_prompt_has_special_member_section(self):
        messages = [Message("1", "1", "u", "魔女公主♪", "hello", 1718000000)]

        prompt = build_summary_prompt("test group", messages)

        self.assertIn("魔女公主♪ 专属", prompt[-1]["content"])

    def test_summary_limit_is_one_batch(self):
        self.assertEqual(DEFAULT_SUMMARY_LIMIT, 500)
        self.assertEqual(MAX_SUMMARY_LIMIT, 500)

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
                if len(self.calls) == 4:
                    return "final summary"
                return f"chunk summary {len(self.calls)}"

        messages = [
            Message(str(i), "1", "u", "user", f"message {i}", 1718000000 + i)
            for i in range(5)
        ]
        client = FakeDeepSeekClient()

        summary = client.summarize("test group", messages)

        self.assertEqual(summary, "final summary")
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(client.calls[-1][0]["role"], "system")


if __name__ == "__main__":
    unittest.main()
