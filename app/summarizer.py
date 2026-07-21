from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .storage import Message


SINGLE_CALL_MAX_CHARS = 18000
CHUNK_MAX_MESSAGES = 250
CHUNK_MAX_CHARS = 12000
MERGE_MAX_BLOCKS = 12
MERGE_MAX_CHARS = 18000
CHUNK_PARALLELISM = 4
SPECIAL_MEMBER_NAME = "魔女公主♪"


class SummarizerError(RuntimeError):
    pass


@dataclass(frozen=True)
class SummaryBlock:
    title: str
    message_count: int
    time_range: str
    content: str


def _format_time(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%m-%d %H:%M")


def _message_time_range(messages: list[Message]) -> str:
    if not messages:
        return "无消息"
    started_at = _format_time(messages[0].timestamp)
    ended_at = _format_time(messages[-1].timestamp)
    if started_at == ended_at:
        return started_at
    return f"{started_at} - {ended_at}"


def _format_message_line(message: Message) -> str:
    content = message.content.replace("\r\n", "\n").replace("\r", "\n").strip()
    content = " / ".join(line.strip() for line in content.splitlines() if line.strip())
    return f"[{_format_time(message.timestamp)}][{message.sender_name}] {content}"


def _formatted_messages_chars(messages: list[Message]) -> int:
    return sum(len(_format_message_line(message)) + 1 for message in messages)


def compact_text(text: str, max_chars: int, marker: str = "\n\n...中间内容省略...\n\n") -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text

    reserved = len(marker)
    budget = max(max_chars - reserved, 200)
    head_budget = budget // 2
    tail_budget = budget - head_budget
    return f"{text[:head_budget].rstrip()}{marker}{text[-tail_budget:].lstrip()}".strip()


def compact_messages(messages: list[Message], max_chars: int = SINGLE_CALL_MAX_CHARS) -> str:
    lines = [_format_message_line(message) for message in messages]
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text

    marker_template = "\n\n...中间省略 {count} 条消息...\n\n"
    marker = marker_template.format(count=len(lines))
    reserved = len(marker)
    budget = max(max_chars - reserved, 200)
    head_budget = budget // 2
    tail_budget = budget - head_budget

    head = text[:head_budget].rstrip()
    tail = text[-tail_budget:].lstrip()
    head_line_count = head.count("\n") + (1 if head else 0)
    tail_line_count = tail.count("\n") + (1 if tail else 0)
    omitted = max(len(lines) - head_line_count - tail_line_count, 0)
    return (
        f"{head}{marker_template.format(count=omitted)}{tail}"
    ).strip()


def split_messages(
    messages: list[Message],
    max_messages: int = CHUNK_MAX_MESSAGES,
    max_chars: int = CHUNK_MAX_CHARS,
) -> list[list[Message]]:
    if max_messages <= 0:
        raise ValueError("max_messages must be greater than 0")
    if max_chars <= 0:
        raise ValueError("max_chars must be greater than 0")

    chunks: list[list[Message]] = []
    current: list[Message] = []
    current_chars = 0

    for message in messages:
        line_chars = len(_format_message_line(message)) + 1
        should_flush = bool(current) and (
            len(current) >= max_messages or current_chars + line_chars > max_chars
        )
        if should_flush:
            chunks.append(current)
            current = []
            current_chars = 0

        current.append(message)
        current_chars += line_chars

    if current:
        chunks.append(current)
    return chunks


def strip_markdown_noise(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def build_summary_prompt(
    group_name: str,
    messages: list[Message],
    max_chars: int = SINGLE_CALL_MAX_CHARS,
) -> list[dict[str, str]]:
    message_text = compact_messages(messages, max_chars=max_chars)
    system = (
        "你是一个谨慎的 QQ 群消息总结助手。"
        "你只根据用户提供的聊天记录总结，不编造不存在的信息。"
        "请以覆盖信息为优先，不要为了简短漏掉具体话题、问题、情绪变化或零散但可能有用的消息。"
        "相似寒暄和重复刷屏可以合并，其他独立信息点尽量保留，清晰中文输出。"
    )
    user = f"""
请总结 QQ 群「{group_name}」中以下 {len(messages)} 条新消息。
输出格式：
## 总览
用 4-8 句话说明这批消息主要在聊什么，要覆盖所有主要话题、明显情绪/争议、重要进展和可能需要用户知道的小变化。
## 重点话题
- 按话题尽量完整列出，不要只列最热门的话题；每个话题说明参与者、主要内容、结论/分歧/状态。
- 只有一两条消息但包含新信息、问题、邀请、安排、链接、文件、提醒或明显情绪的，也要列出。
- 如果内容很零散，可以增加“零散但可能有用的信息”小项集中列出。
## 待办/决定
- 列出明确的待办、负责人、时间点；没有就写“暂无明确待办”。
## 需要关注
- 列出 @我、重要链接、文件、风险、争议或可能需要回复的内容；没有就写“暂无特别需要关注”。
## {SPECIAL_MEMBER_NAME} 专属
- 单独总结和“{SPECIAL_MEMBER_NAME}”相关的内容：她本人发的消息、别人回复她的消息、@她/提到她的消息、围绕她的安排/问题/争议/需要回复事项。
- 需要尽量保留具体上下文、被回复内容、相关人员和可能需要她注意的点；没有相关内容就写“暂无和 {SPECIAL_MEMBER_NAME} 直接相关的内容”。
## 消息范围
- 简短说明消息数量和时间范围：{_message_time_range(messages)}。

聊天记录：
{message_text}
""".strip()
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_chunk_summary_prompt(
    group_name: str,
    messages: list[Message],
    chunk_index: int,
    total_chunks: int,
    max_chars: int = CHUNK_MAX_CHARS,
) -> list[dict[str, str]]:
    message_text = compact_messages(messages, max_chars=max_chars)
    system = (
        "你是一个 QQ 群消息分块总结助手。"
        "当前任务只总结提供给你的这一块聊天记录，不要推测其他分块的内容。"
        "当前分块摘要会作为最终总结的唯一依据，所以要优先保留细节覆盖率。"
        "保留关键人物、独立话题、低频但具体的信息、决定、待办、风险、链接/文件线索和需要回复的事项。"
    )
    user = f"""
请总结 QQ 群「{group_name}」的第 {chunk_index}/{total_chunks} 块消息。
本块共 {len(messages)} 条，时间范围：{_message_time_range(messages)}。

输出要求：
- 用清晰中文输出，可以适当详细，避免漏掉独立信息点。
- 按“本块概览 / 重点话题 / 零散但可能有用的信息 / 待办或决定 / 需要关注 / {SPECIAL_MEMBER_NAME} 专属 / 时间范围”组织。
- “重点话题”尽量覆盖本块内出现过的不同话题；重复表情、玩笑和刷屏可合并，但不要删除有新含义的消息。
- “{SPECIAL_MEMBER_NAME} 专属”要单独保留她本人发言、别人回复她、@她/提到她、围绕她的安排/问题/争议/需要回复事项；没有就写“暂无”。
- 不要输出最终总结，只输出本块摘要，方便之后合并。

聊天记录：
{message_text}
""".strip()
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _summary_blocks_text(blocks: list[SummaryBlock], max_chars: int | None = None) -> str:
    parts = []
    total = len(blocks)
    for index, block in enumerate(blocks, start=1):
        parts.append(
            "\n".join(
                [
                    f"### 分块 {index}/{total}：{block.title}",
                    f"- 消息数：{block.message_count}",
                    f"- 时间范围：{block.time_range}",
                    block.content.strip(),
                ]
            )
        )
    text = "\n\n".join(parts)
    if max_chars is None:
        return text
    return compact_text(text, max_chars=max_chars, marker="\n\n...中间分块总结省略...\n\n")


def build_merge_summary_prompt(
    group_name: str,
    blocks: list[SummaryBlock],
    total_message_count: int,
    total_chunk_count: int,
    final: bool,
    max_chars: int = MERGE_MAX_CHARS,
) -> list[dict[str, str]]:
    block_text = _summary_blocks_text(blocks, max_chars=max_chars)
    system = (
        "你是一个 QQ 群消息总结合并助手。"
        "你只根据分块摘要合并信息，不编造不存在的信息。"
        "合并时要去重，把重复话题归并到一起，但不要因为追求简短而丢弃低频但具体的信息。"
        "尤其要保留待办、决定、问题、邀请、安排、风险、链接/文件线索、需要回复的事项和明显情绪变化。"
    )
    if final:
        task = "请把以下分块摘要合并为最终总结。"
        output = f"""
输出格式：
## 总览
用 4-8 句话说明整体主要在聊什么，覆盖主要话题、次要但具体的话题、重要情绪/争议和关键进展。
## 重点话题
- 按话题合并列出，尽量覆盖所有独立信息点，不只列最热门的话题。
- 每个话题说明参与者、主要内容、结论/分歧/状态；如果只是闲聊，也简要说明聊了什么和是否有需要关注的点。
- 对零散但可能有用的信息，单独列“零散但可能有用的信息”。
## 待办/决定
- 合并明确的待办、负责人、时间点；没有就写“暂无明确待办”。
## 需要关注
- 合并 @我、重要链接、文件、风险、争议或可能需要回复的内容；没有就写“暂无特别需要关注”。
## {SPECIAL_MEMBER_NAME} 专属
- 合并所有分块里和“{SPECIAL_MEMBER_NAME}”相关的内容：她本人发言、别人回复她、@她/提到她、围绕她的安排/问题/争议/需要回复事项。
- 尽量保留具体上下文、被回复内容、相关人员、结论/状态和可能需要她注意或回复的点；没有相关内容就写“暂无和 {SPECIAL_MEMBER_NAME} 直接相关的内容”。
## 消息范围
- 说明本次共 {total_message_count} 条消息，采用 {total_chunk_count} 个分块处理，并概括时间范围。
""".strip()
    else:
        task = "请把以下较小的分块摘要先合并成一个中间摘要，供最终总结继续合并。"
        output = f"""
输出要求：
- 保持简洁中文。
- 保留所有独立话题、明确待办、决定、问题、安排、风险、链接/文件线索和需要回复的事项。
- 保留所有和“{SPECIAL_MEMBER_NAME}”相关的信息，后续最终总结需要单独生成“{SPECIAL_MEMBER_NAME} 专属”小节。
- 合并重复话题，但不要丢弃相互冲突的观点。
""".strip()

    user = f"""
{task}
QQ群：{group_name}
本次总消息数：{total_message_count}
原始分块数：{total_chunk_count}
当前待合并摘要数：{len(blocks)}

{output}

分块摘要：
{block_text}
""".strip()
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def split_summary_blocks(
    blocks: list[SummaryBlock],
    max_blocks: int = MERGE_MAX_BLOCKS,
    max_chars: int = MERGE_MAX_CHARS,
) -> list[list[SummaryBlock]]:
    batches: list[list[SummaryBlock]] = []
    current: list[SummaryBlock] = []
    current_chars = 0

    for block in blocks:
        block_chars = len(block.content) + len(block.title) + len(block.time_range) + 80
        should_flush = bool(current) and (
            len(current) >= max_blocks or current_chars + block_chars > max_chars
        )
        if should_flush:
            batches.append(current)
            current = []
            current_chars = 0

        current.append(block)
        current_chars += block_chars

    if current:
        batches.append(current)
    return batches


class DeepSeekClient:
    def __init__(
        self,
        api_key: str | None,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-flash",
        timeout: int = 60,
        single_call_max_chars: int = SINGLE_CALL_MAX_CHARS,
        chunk_max_messages: int = CHUNK_MAX_MESSAGES,
        chunk_max_chars: int = CHUNK_MAX_CHARS,
        merge_max_blocks: int = MERGE_MAX_BLOCKS,
        merge_max_chars: int = MERGE_MAX_CHARS,
        chunk_parallelism: int = CHUNK_PARALLELISM,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.single_call_max_chars = single_call_max_chars
        self.chunk_max_messages = chunk_max_messages
        self.chunk_max_chars = chunk_max_chars
        self.merge_max_blocks = merge_max_blocks
        self.merge_max_chars = merge_max_chars
        self.chunk_parallelism = max(int(chunk_parallelism), 1)

    def summarize(
        self,
        group_name: str,
        messages: list[Message],
        existing_blocks: dict[int, SummaryBlock] | None = None,
        plan_callback: Callable[[int], None] | None = None,
        chunk_callback: Callable[[int, SummaryBlock], None] | None = None,
        stage_callback: Callable[[str], None] | None = None,
    ) -> str:
        if not self.api_key:
            raise SummarizerError("Missing DEEPSEEK_API_KEY environment variable")
        if not messages:
            raise SummarizerError("No messages to summarize")

        saved_blocks = existing_blocks or {}
        can_use_single_call = (
            len(messages) <= self.chunk_max_messages
            and _formatted_messages_chars(messages) <= self.single_call_max_chars
        )
        if can_use_single_call:
            if plan_callback:
                plan_callback(1)
            saved = saved_blocks.get(1)
            if saved is not None:
                return saved.content
            summary = self._chat(
                build_summary_prompt(
                    group_name,
                    messages,
                    max_chars=self.single_call_max_chars,
                )
            )
            block = SummaryBlock(
                title="完整摘要",
                message_count=len(messages),
                time_range=_message_time_range(messages),
                content=summary,
            )
            if chunk_callback:
                chunk_callback(1, block)
            return summary

        chunks = split_messages(
            messages,
            max_messages=self.chunk_max_messages,
            max_chars=self.chunk_max_chars,
        )
        if len(chunks) == 1:
            if plan_callback:
                plan_callback(1)
            saved = saved_blocks.get(1)
            if saved is not None:
                return saved.content
            summary = self._chat(
                build_summary_prompt(
                    group_name,
                    chunks[0],
                    max_chars=max(self.single_call_max_chars, self.chunk_max_chars),
                )
            )
            block = SummaryBlock(
                title="完整摘要",
                message_count=len(chunks[0]),
                time_range=_message_time_range(chunks[0]),
                content=summary,
            )
            if chunk_callback:
                chunk_callback(1, block)
            return summary

        if plan_callback:
            plan_callback(len(chunks))

        def summarize_chunk(item: tuple[int, list[Message]]) -> SummaryBlock:
            index, chunk = item
            chunk_summary = self._chat(
                build_chunk_summary_prompt(
                    group_name,
                    chunk,
                    chunk_index=index,
                    total_chunks=len(chunks),
                    max_chars=self.chunk_max_chars,
                )
            )
            block = SummaryBlock(
                title=f"第 {index}/{len(chunks)} 块",
                message_count=len(chunk),
                time_range=_message_time_range(chunk),
                content=chunk_summary,
            )
            if chunk_callback:
                chunk_callback(index, block)
            return block

        indexed_chunks = list(enumerate(chunks, start=1))
        blocks_by_index = {
            index: block
            for index, block in saved_blocks.items()
            if 1 <= index <= len(chunks)
        }
        pending_chunks = [item for item in indexed_chunks if item[0] not in blocks_by_index]
        if pending_chunks:
            worker_count = min(self.chunk_parallelism, len(pending_chunks))
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="summary-chunk") as executor:
                completed = list(executor.map(summarize_chunk, pending_chunks))
            for (index, _), block in zip(pending_chunks, completed):
                blocks_by_index[index] = block

        blocks = [blocks_by_index[index] for index in range(1, len(chunks) + 1)]
        if stage_callback:
            stage_callback("merging")

        return self._merge_summary_blocks(
            group_name=group_name,
            blocks=blocks,
            total_message_count=len(messages),
            total_chunk_count=len(chunks),
        )

    def _merge_summary_blocks(
        self,
        group_name: str,
        blocks: list[SummaryBlock],
        total_message_count: int,
        total_chunk_count: int,
    ) -> str:
        if len(blocks) <= self.merge_max_blocks and len(_summary_blocks_text(blocks)) <= self.merge_max_chars:
            return self._chat(
                build_merge_summary_prompt(
                    group_name=group_name,
                    blocks=blocks,
                    total_message_count=total_message_count,
                    total_chunk_count=total_chunk_count,
                    final=True,
                    max_chars=self.merge_max_chars,
                )
            )

        batches = split_summary_blocks(
            blocks,
            max_blocks=self.merge_max_blocks,
            max_chars=self.merge_max_chars,
        )
        if len(batches) == 1:
            return self._chat(
                build_merge_summary_prompt(
                    group_name=group_name,
                    blocks=blocks,
                    total_message_count=total_message_count,
                    total_chunk_count=total_chunk_count,
                    final=True,
                    max_chars=self.merge_max_chars,
                )
            )

        reduced_blocks: list[SummaryBlock] = []
        for index, batch in enumerate(batches, start=1):
            merged = self._chat(
                build_merge_summary_prompt(
                    group_name=group_name,
                    blocks=batch,
                    total_message_count=sum(block.message_count for block in batch),
                    total_chunk_count=total_chunk_count,
                    final=False,
                    max_chars=self.merge_max_chars,
                )
            )
            reduced_blocks.append(
                SummaryBlock(
                    title=f"合并块 {index}/{len(batches)}",
                    message_count=sum(block.message_count for block in batch),
                    time_range=f"{batch[0].time_range}；{batch[-1].time_range}",
                    content=merged,
                )
            )

        return self._merge_summary_blocks(
            group_name=group_name,
            blocks=reduced_blocks,
            total_message_count=total_message_count,
            total_chunk_count=total_chunk_count,
        )

    def _chat(self, messages: list[dict[str, str]]) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 4096,
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
            elapsed = time.time() - started_at
            raise SummarizerError(f"Unexpected DeepSeek API response after {elapsed:.1f}s") from exc

        return strip_markdown_noise(str(content))
