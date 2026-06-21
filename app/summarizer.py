from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

from .storage import Message


class SummarizerError(RuntimeError):
    pass


def _format_time(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%m-%d %H:%M")


def compact_messages(messages: list[Message], max_chars: int = 18000) -> str:
    lines = [
        f"[{_format_time(message.timestamp)}][{message.sender_name}] {message.content}"
        for message in messages
    ]
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text

    marker = "\n\n...中间省略 {count} 条消息...\n\n"
    reserved = len(marker.format(count=len(lines)))
    budget = max(max_chars - reserved, 200)
    head_budget = budget // 2
    tail_budget = budget - head_budget

    head_lines: list[str] = []
    used = 0
    for line in lines:
        line_len = len(line) + 1
        if head_lines and used + line_len > head_budget:
            break
        head_lines.append(line)
        used += line_len

    tail_lines: list[str] = []
    used = 0
    for line in reversed(lines):
        line_len = len(line) + 1
        if tail_lines and used + line_len > tail_budget:
            break
        tail_lines.append(line)
        used += line_len
    tail_lines.reverse()

    omitted = max(len(lines) - len(head_lines) - len(tail_lines), 0)
    return (
        "\n".join(head_lines)
        + marker.format(count=omitted)
        + "\n".join(tail_lines)
    ).strip()


def strip_markdown_noise(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def build_summary_prompt(group_name: str, messages: list[Message]) -> list[dict[str, str]]:
    message_text = compact_messages(messages)
    system = (
        "你是一个谨慎的QQ群消息总结助手。"
        "你只根据用户提供的聊天记录总结，不编造不存在的信息。"
        "请优先提炼对用户有行动价值的信息，保持简洁、清晰、中文输出。"
    )
    user = f"""
请总结QQ群「{group_name}」中以下 {len(messages)} 条新消息。

输出格式：
## 总览
用2-4句话说明这批消息主要在聊什么。

## 重点话题
- 按话题列出，每个话题说明结论、分歧或状态。

## 待办/决定
- 列出明确的待办、负责人、时间点；没有就写“暂无明确待办”。

## 需要关注
- 列出@我、重要链接、文件、风险、争议或可能需要回复的内容；没有就写“暂无特别需要关注”。

## 消息范围
- 简短说明消息数量和时间范围。

聊天记录：
{message_text}
""".strip()
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


class DeepSeekClient:
    def __init__(
        self,
        api_key: str | None,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-flash",
        timeout: int = 60,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    def summarize(self, group_name: str, messages: list[Message]) -> str:
        if not self.api_key:
            raise SummarizerError("Missing DEEPSEEK_API_KEY environment variable")
        if not messages:
            raise SummarizerError("No messages to summarize")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": build_summary_prompt(group_name, messages),
            "temperature": 0.2,
            "stream": False,
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        started_at = time.time()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SummarizerError(f"DeepSeek API returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise SummarizerError(f"DeepSeek API request failed: {exc.reason}") from exc

        try:
            data = json.loads(body)
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise SummarizerError(f"Unexpected DeepSeek API response after {time.time() - started_at:.1f}s") from exc

        return strip_markdown_noise(str(content))
