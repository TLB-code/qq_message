import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.onebot import parse_group_message
from app.storage import Message, Store
from app.summarizer import compact_messages


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

    def test_compact_messages_keeps_content_under_limit(self):
        messages = [
            Message(str(i), "1", "u", "群友", f"消息 {i} " + "x" * 50, 1718000000 + i)
            for i in range(400)
        ]

        compacted = compact_messages(messages, max_chars=2000)

        self.assertLessEqual(len(compacted), 2200)
        self.assertIn("中间省略", compacted)


if __name__ == "__main__":
    unittest.main()
