from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import load_settings
from app.onebot import message_to_summary_text, special_member_relations
from app.storage import Message
from app.summarizer import DeepSeekClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize an exported QQ message JSON file.")
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--group-name", default="魔女奏奏游戏群")
    parser.add_argument("--special-user-id", default="2829556413")
    parser.add_argument("--special-name", default="魔女公主♪")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    records = json.loads(args.input.read_text(encoding="utf-8-sig"))[: args.limit]
    source_messages = [
        Message(
            message_id=str(record["message_id"]),
            group_id=str(record["group_id"]),
            user_id=str(record["user_id"]),
            sender_name=str(record["sender_name"]),
            content=str(record["content"]),
            timestamp=int(record["timestamp"]),
            position=index,
        )
        for index, record in enumerate(records, start=1)
    ]
    messages_by_id = {message.message_id: message for message in source_messages}

    summary_messages = []
    for record, source in zip(records, source_messages):
        raw_event = record.get("raw_json") or {}
        if isinstance(raw_event, str):
            raw_event = json.loads(raw_event)
        raw_message = raw_event.get("raw_message")
        message = raw_event.get("message")
        content = message_to_summary_text(
            raw_message,
            message,
            fallback_content=source.content,
            reply_lookup=messages_by_id.get,
        )
        if not content:
            continue
        mentions_special, replies_to_special = special_member_relations(
            raw_message,
            message,
            args.special_user_id,
            reply_lookup=messages_by_id.get,
        )
        summary_messages.append(
            Message(
                message_id=source.message_id,
                group_id=source.group_id,
                user_id=source.user_id,
                sender_name=source.sender_name,
                content=content,
                timestamp=source.timestamp,
                position=source.position,
                is_special_sender=source.user_id == args.special_user_id,
                mentions_special=mentions_special,
                replies_to_special=replies_to_special,
            )
        )

    print(f"raw_messages={len(source_messages)}")
    print(f"effective_messages={len(summary_messages)}")
    if args.dry_run:
        return
    if args.output is None:
        raise SystemExit("--output is required unless --dry-run is used")

    settings = load_settings()
    client = DeepSeekClient(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        timeout=settings.deepseek_timeout,
        request_retries=settings.deepseek_request_retries,
        special_member_user_id=args.special_user_id,
        special_member_display_name=args.special_name,
    )
    summary = client.summarize(
        args.group_name,
        summary_messages,
        source_messages=source_messages,
    )
    args.output.write_text(summary + "\n", encoding="utf-8")
    print(f"output={args.output}")


if __name__ == "__main__":
    main()
