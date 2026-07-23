from __future__ import annotations

import argparse
from pathlib import Path

from app.config import load_settings
from app.storage import Message
from app.summarizer import DeepSeekClient


SPECIAL_USER_ID = "special-qq"


def build_messages() -> list[Message]:
    timestamp = 1784777280
    messages = [
        Message(str(index), "test-group", f"user-{index}", f"测试成员{index}", "普通签到", timestamp + index, position=index)
        for index in range(1, 14)
    ]
    messages[0] = Message("1", "test-group", "cloud-user", "云梦", "普通签到", timestamp + 1, position=1)
    messages[8] = Message("9", "test-group", "alias-nine-user", "别名九号", "普通签到", timestamp + 9, position=9)
    messages[12] = Message("13", "test-group", "nickname-009-user", "009", "我不再参加上贡积分玩笑了", timestamp + 13, position=13)

    for position in range(14, 501):
        user_id = f"regular-{position % 18}"
        sender_name = f"群友{position % 18}"
        content = f"普通闲聊 {position}"
        kwargs = {}
        if position == 40:
            user_id, sender_name, content = "photo-user", "可喵", "照片怕被盗图，可以加水印"
        elif position == 41:
            user_id, sender_name, content = "reply-user", "月月", "那就私发"
        elif position == 80:
            user_id, sender_name, content = "game-user", "玛凡", "那位勇者欠800积分又赌800"
        elif position == 81:
            user_id, sender_name, content = "kiku-user", "KIKU", "好好好赌狗"
        elif position == 82:
            user_id, sender_name, content = "leaf-user", "三叶", "好坏喵"
        elif position == 120:
            user_id, sender_name, content = "chess-user", "浅浅", "老大，我下不过绵绵小姐"
        elif position == 121:
            user_id, sender_name, content = "chess-rival", "绵绵", "杂鱼"
        elif position == 122:
            user_id, sender_name, content = "chess-user", "浅浅", "可恶！！"
        elif position == 123:
            user_id, sender_name, content = "chess-rival", "绵绵", "退出象棋社吧"
        elif position == 170:
            user_id, sender_name, content = "food-user", "杜谟里", "周末再去补一波货"
        elif position == 220:
            user_id, sender_name, content = "question-user", "小恋", "奏咪在第几组？"
        elif position == 221:
            user_id, sender_name, content = "organizer-user", "组织者", "奏咪在第一组，往前翻就能看到"
        elif position in {260, 280, 300, 320, 340, 360, 380, 400}:
            user_id, sender_name = "organizer-user", "组织者"
            content = f"继续收集第三组投票名单，目前进度 {position}"
        elif position in {270, 290, 310, 330, 350, 370, 390, 410}:
            user_id, sender_name, content = f"voter-{position}", f"投票成员{position}", "提名一位候选人"
        elif position == 420:
            user_id, sender_name, content = "organizer-user", "组织者", "第三组十人名单已经确认"
        elif position == 421:
            user_id, sender_name, content = "object-user", "异议成员", "名单里有我不合适，我有异议"
        elif position == 450:
            user_id, sender_name, content = SPECIAL_USER_ID, "魔女公主♪", "欢迎新成员"
            kwargs["is_special_sender"] = True
        elif position == 451:
            user_id, sender_name, content = "spoof-user", "魔女公主♪（伪）", "我才是重点成员"
        elif position == 470:
            user_id, sender_name, content = "mention-user", "群友A", "@重点成员 抱抱"
            kwargs["mentions_special"] = True
        messages.append(
            Message(
                str(position),
                "test-group",
                user_id,
                sender_name,
                content,
                timestamp + position,
                position=position,
                **kwargs,
            )
        )
    return messages


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = load_settings()
    messages = build_messages()
    client = DeepSeekClient(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        timeout=settings.deepseek_timeout,
        request_retries=settings.deepseek_request_retries,
        special_member_user_id=SPECIAL_USER_ID,
        special_member_display_name="魔女公主♪",
    )
    summary = client.summarize("合成测试群", messages, source_messages=messages)
    args.output.write_text(summary + "\n", encoding="utf-8")
    print(f"messages={len(messages)}")
    print(f"output={args.output}")


if __name__ == "__main__":
    main()
