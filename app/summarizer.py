from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable

from .storage import Message


SINGLE_CALL_MAX_CHARS = 18000
CHUNK_MAX_MESSAGES = 250
CHUNK_MAX_CHARS = 12000
MERGE_MAX_BLOCKS = 12
MERGE_MAX_CHARS = 18000
CHUNK_PARALLELISM = 4
SUMMARY_PIPELINE_VERSION = 2
DEFAULT_SPECIAL_MEMBER_NAME = "魔女公主♪"
SUMMARY_SECTIONS = ("topics", "actions", "attention", "special_member")
EVIDENCE_TYPES = {
    "confirmed_fact",
    "confirmed_plan",
    "proposal",
    "banter",
    "self_report",
    "reported_concern",
    "disagreement",
    "unknown",
}
NAMED_QQ_MENTION_PATTERN = re.compile(r"@([^()\s]{1,60})\((\d{5,12})\)")
QQ_MENTION_PATTERN = re.compile(r"@(\d{5,12})\b")


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
    position = f"#{message.position}" if message.position else "#?"
    alias = message.sender_alias or "成员"
    relations = []
    if message.is_special_sender:
        relations.append("重点成员本人")
    if message.mentions_special:
        relations.append("@重点成员")
    if message.replies_to_special:
        relations.append("回复重点成员")
    relation_text = f"[{'/'.join(relations)}]" if relations else ""
    return (
        f"[{position}][{_format_time(message.timestamp)}]"
        f"[{alias}][当前昵称：{message.sender_name}]{relation_text} {content}"
    )


def prepare_messages(messages: list[Message], special_user_id: str | None) -> list[Message]:
    aliases: dict[str, str] = {}
    for message in messages:
        if special_user_id and message.user_id == special_user_id:
            continue
        aliases.setdefault(message.user_id, f"成员{len(aliases) + 1:03d}")

    def mention_alias(user_id: str) -> str:
        if special_user_id and user_id == special_user_id:
            return "重点成员"
        return aliases.get(user_id, "某成员")

    prepared = []
    for index, message in enumerate(messages, start=1):
        if special_user_id and message.user_id == special_user_id:
            alias = "重点成员"
        else:
            alias = aliases[message.user_id]
        content = NAMED_QQ_MENTION_PATTERN.sub(
            lambda match: f"@{mention_alias(match.group(2))}（{match.group(1)}）",
            message.content,
        )
        content = QQ_MENTION_PATTERN.sub(
            lambda match: f"@{mention_alias(match.group(1))}",
            content,
        )
        prepared.append(
            replace(
                message,
                content=content,
                position=message.position or index,
                sender_alias=alias,
                is_special_sender=bool(
                    message.is_special_sender
                    or (special_user_id and message.user_id == special_user_id)
                ),
            )
        )
    return prepared


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
    special_member_name: str = DEFAULT_SPECIAL_MEMBER_NAME,
) -> list[dict[str, str]]:
    return build_chunk_summary_prompt(
        group_name,
        messages,
        chunk_index=1,
        total_chunks=1,
        max_chars=max_chars,
        special_member_name=special_member_name,
    )


def build_chunk_summary_prompt(
    group_name: str,
    messages: list[Message],
    chunk_index: int,
    total_chunks: int,
    max_chars: int = CHUNK_MAX_CHARS,
    special_member_name: str = DEFAULT_SPECIAL_MEMBER_NAME,
) -> list[dict[str, str]]:
    message_text = compact_messages(messages, max_chars=max_chars)
    system = (
        "你是谨慎的 QQ 群消息事实提取助手。"
        "只能依据提供的聊天记录，不得补充常识推断、诊断、动机或现实身份。"
        "每个条目必须引用本块中真实存在的消息位置。"
    )
    user = f"""
请总结 QQ 群「{group_name}」的第 {chunk_index}/{total_chunks} 块消息。
本块共 {len(messages)} 条，时间范围：{_message_time_range(messages)}。

只输出一个 JSON 对象，不要使用 Markdown 代码块。固定结构：
{{
  "topics": [条目],
  "actions": [条目],
  "attention": [条目],
  "special_member": [条目]
}}
每个条目固定结构：
{{
  "title": "简短标题",
  "type": "证据类型",
  "status": "明确状态，没有则为空字符串",
  "summary": "严格依据原消息的中文归纳",
  "participants": ["成员别名"],
  "evidence": [消息位置数字]
}}

证据类型只能使用：confirmed_fact、confirmed_plan、proposal、banter、self_report、reported_concern、disagreement、unknown。
规则：
- 明确发生的事实用 confirmed_fact；明确承诺或安排才用 confirmed_plan；建议和意向用 proposal。
- 玩笑、跟风、角色扮演、暧昧话术用 banter，绝不能改写成真实计划或风险。
- 成员对账号、金额、经历和健康的自述用 self_report，不得写成已核实事实。
- 别人表达担忧用 reported_concern，不得升级为客观健康风险或诊断。
- 不得把“没人回应”“尚未解决”“存在风险”作为结论，除非消息明确说明。
- 昵称可能是角色扮演，不得映射到现实同名人物；不得改变原词含义，例如“黄油”不能写成“黄图”。
- actions 只放 confirmed_plan 或 proposal；attention 只放确实需要回复、明确争议或明确担忧的内容。
- special_member 只允许引用带“重点成员本人”“@重点成员”或“回复重点成员”标记的消息。
- “{special_member_name}”只是显示名称，重点成员身份只由上述标记确定。相似昵称、带“伪”的昵称都不是身份依据。
- 合并重复刷屏，但尽量覆盖有实际信息的独立话题。每个条目保留 1-5 个最直接的证据位置。

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
    return text


def build_merge_summary_prompt(
    group_name: str,
    blocks: list[SummaryBlock],
    total_message_count: int,
    total_chunk_count: int,
    final: bool,
    max_chars: int = MERGE_MAX_CHARS,
    special_member_name: str = DEFAULT_SPECIAL_MEMBER_NAME,
) -> list[dict[str, str]]:
    block_text = _summary_blocks_text(blocks)
    system = (
        "你是 QQ 群结构化事实合并助手。"
        "只能合并输入 JSON 中已有的信息，所有结论必须继续保留原始消息位置。"
        "不得提升证据强度，不得把玩笑变成计划、把自述变成事实、把担忧变成客观风险。"
    )
    task = "请合并为最终结构" if final else "请合并为中间结构"
    output = f"""
只输出 JSON，不要输出 Markdown。输出结构与输入完全相同：topics、actions、attention、special_member 四个数组。
- 合并同一事件并合并 evidence，冲突观点必须保留，证据位置必须是输入中已有的数字。
- type 只能是：{', '.join(sorted(EVIDENCE_TYPES))}。
- confirmed_plan、proposal、banter、self_report、reported_concern、disagreement 不能在合并时升级为 confirmed_fact。
- actions 只放 confirmed_plan 或 proposal。
- special_member 只能保留原来已在 special_member 中的条目，不得根据昵称重新识别。
- “{special_member_name}”仅为显示名称，不代表昵称相似者。
- 每个条目保留 1-8 个最直接的证据位置；不要生成消息中不存在的状态。
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


def parse_summary_json(
    value: str,
    allowed_positions: set[int],
    special_positions: set[int],
) -> dict[str, list[dict[str, Any]]]:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        # Compatible endpoints may still wrap JSON output in a short explanation.
        # Decode the first complete object without weakening the schema checks below.
        object_start = text.find("{")
        if object_start < 0:
            raise SummarizerError("DeepSeek did not return valid summary JSON") from exc
        try:
            payload, _ = json.JSONDecoder().raw_decode(text[object_start:])
        except json.JSONDecodeError as nested_exc:
            raise SummarizerError("DeepSeek did not return valid summary JSON") from nested_exc
    if not isinstance(payload, dict):
        raise SummarizerError("DeepSeek summary JSON must be an object")

    normalized: dict[str, list[dict[str, Any]]] = {}
    for section in SUMMARY_SECTIONS:
        raw_items = payload.get(section, [])
        if not isinstance(raw_items, list):
            raise SummarizerError(f"Summary section {section} must be an array")
        items = []
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                raise SummarizerError(f"Summary section {section} contains a non-object item")
            item_type = str(raw_item.get("type") or "unknown").strip()
            if item_type not in EVIDENCE_TYPES:
                raise SummarizerError(f"Unsupported evidence type: {item_type}")
            if section == "actions" and item_type not in {"confirmed_plan", "proposal"}:
                raise SummarizerError("Actions may only contain confirmed_plan or proposal items")
            evidence_raw = raw_item.get("evidence")
            if not isinstance(evidence_raw, list) or not evidence_raw:
                raise SummarizerError("Every summary item must contain evidence positions")
            try:
                evidence = list(dict.fromkeys(int(position) for position in evidence_raw))
            except (TypeError, ValueError) as exc:
                raise SummarizerError("Evidence positions must be integers") from exc
            if any(position not in allowed_positions for position in evidence):
                raise SummarizerError("Summary referenced a message outside the provided input")
            if section == "special_member" and not special_positions.intersection(evidence):
                raise SummarizerError("Special member item has no QQ-verified evidence")

            title = str(raw_item.get("title") or "").strip()
            summary = str(raw_item.get("summary") or "").strip()
            if not title or not summary:
                raise SummarizerError("Every summary item needs a title and summary")
            participants_raw = raw_item.get("participants") or []
            participants = (
                [str(participant).strip() for participant in participants_raw if str(participant).strip()]
                if isinstance(participants_raw, list)
                else []
            )
            items.append(
                {
                    "title": title,
                    "type": item_type,
                    "status": str(raw_item.get("status") or "").strip(),
                    "summary": summary,
                    "participants": list(dict.fromkeys(participants))[:12],
                    "evidence": evidence[:8],
                }
            )
        normalized[section] = items
    return normalized


def summary_json_text(payload: dict[str, list[dict[str, Any]]]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _evidence_label(item: dict[str, Any], messages_by_position: dict[int, Message]) -> str:
    labels = []
    for position in item["evidence"]:
        message = messages_by_position.get(int(position))
        if message is None:
            continue
        labels.append(f"#{position} {_format_time(message.timestamp)}")
    return "、".join(labels)


def _render_items(
    items: list[dict[str, Any]],
    messages_by_position: dict[int, Message],
    participant_names: dict[str, str],
    empty_text: str,
) -> list[str]:
    if not items:
        return [empty_text]
    lines = []
    type_labels = {
        "confirmed_fact": "已确认事实",
        "confirmed_plan": "明确安排",
        "proposal": "提议/意向",
        "banter": "玩笑互动",
        "self_report": "成员自述",
        "reported_concern": "群友担忧",
        "disagreement": "存在分歧",
        "unknown": "信息不足",
    }
    for item in items:
        details = [type_labels[item["type"]]]
        if item["status"]:
            details.append(item["status"])
        participants = "、".join(
            (
                f"{participant}（{participant_names[participant]}）"
                if participant in participant_names
                else participant
            )
            for participant in item["participants"]
        )
        if participants:
            details.append(f"参与者：{participants}")
        evidence = _evidence_label(item, messages_by_position)
        lines.append(
            f"**{item['title']}**（{'；'.join(details)}）：{item['summary']}"
            + (f" [证据：{evidence}]" if evidence else "")
        )
    return lines


def render_summary(
    payload: dict[str, list[dict[str, Any]]],
    summary_messages: list[Message],
    source_messages: list[Message],
    total_chunk_count: int,
    special_member_name: str,
) -> str:
    messages_by_position = {message.position: message for message in summary_messages}
    participant_names = {
        message.sender_alias: message.sender_name
        for message in summary_messages
        if message.sender_alias
    }
    topic_items = payload["topics"]
    overview = [item["summary"] for item in topic_items[:6]]
    lines = ["## 总览"]
    lines.extend(f"- {item}" for item in overview or ["本批消息没有可确认的文本话题。"])
    lines.extend(["", "## 重点话题"])
    lines.extend(
        _render_items(
            topic_items,
            messages_by_position,
            participant_names,
            "暂无明确重点话题。",
        )
    )
    lines.extend(["", "## 待办/决定"])
    lines.extend(
        _render_items(
            payload["actions"],
            messages_by_position,
            participant_names,
            "暂无明确待办。",
        )
    )
    lines.extend(["", "## 需要关注"])
    lines.extend(
        _render_items(
            payload["attention"],
            messages_by_position,
            participant_names,
            "暂无特别需要关注。",
        )
    )
    lines.extend(["", f"## {special_member_name} 专属"])
    lines.extend(
        _render_items(
            payload["special_member"],
            messages_by_position,
            participant_names,
            f"暂无与 {special_member_name} QQ 身份直接关联的内容。",
        )
    )

    range_messages = source_messages or summary_messages
    time_range = _message_time_range(range_messages)
    max_gap_seconds = 0
    if len(range_messages) > 1:
        max_gap_seconds = max(
            max(current.timestamp - previous.timestamp, 0)
            for previous, current in zip(range_messages, range_messages[1:])
        )
    gap_minutes, gap_seconds = divmod(max_gap_seconds, 60)
    lines.extend(
        [
            "",
            "## 消息范围",
            f"- 原始消息 {len(source_messages)} 条，其中有效文本消息 {len(summary_messages)} 条。",
            f"- 使用 {total_chunk_count} 个分块，时间范围：{time_range}。",
            f"- 相邻消息最长间隔：{gap_minutes} 分 {gap_seconds} 秒（由程序计算）。",
        ]
    )
    return "\n".join(lines).strip()


class DeepSeekClient:
    def __init__(
        self,
        api_key: str | None,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-flash",
        timeout: int = 180,
        request_retries: int = 2,
        single_call_max_chars: int = SINGLE_CALL_MAX_CHARS,
        chunk_max_messages: int = CHUNK_MAX_MESSAGES,
        chunk_max_chars: int = CHUNK_MAX_CHARS,
        merge_max_blocks: int = MERGE_MAX_BLOCKS,
        merge_max_chars: int = MERGE_MAX_CHARS,
        chunk_parallelism: int = CHUNK_PARALLELISM,
        special_member_user_id: str | None = None,
        special_member_display_name: str = DEFAULT_SPECIAL_MEMBER_NAME,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.request_retries = max(int(request_retries), 0)
        self.single_call_max_chars = single_call_max_chars
        self.chunk_max_messages = chunk_max_messages
        self.chunk_max_chars = chunk_max_chars
        self.merge_max_blocks = merge_max_blocks
        self.merge_max_chars = merge_max_chars
        self.chunk_parallelism = max(int(chunk_parallelism), 1)
        self.special_member_user_id = special_member_user_id
        self.special_member_display_name = special_member_display_name

    def summarize(
        self,
        group_name: str,
        messages: list[Message],
        existing_blocks: dict[int, SummaryBlock] | None = None,
        plan_callback: Callable[[int], None] | None = None,
        chunk_callback: Callable[[int, SummaryBlock], None] | None = None,
        stage_callback: Callable[[str], None] | None = None,
        source_messages: list[Message] | None = None,
    ) -> str:
        if not self.api_key:
            raise SummarizerError("Missing DEEPSEEK_API_KEY environment variable")
        if not messages:
            raise SummarizerError("No messages to summarize")

        messages = prepare_messages(messages, self.special_member_user_id)
        source_messages = source_messages or messages
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
                payload = parse_summary_json(
                    saved.content,
                    {message.position for message in messages},
                    self._special_positions(messages),
                )
                return render_summary(
                    payload,
                    messages,
                    source_messages,
                    1,
                    self.special_member_display_name,
                )
            payload = self._chat_structured(
                build_summary_prompt(
                    group_name,
                    messages,
                    max_chars=self.single_call_max_chars,
                    special_member_name=self.special_member_display_name,
                ),
                messages,
            )
            block = SummaryBlock(
                title="完整摘要",
                message_count=len(messages),
                time_range=_message_time_range(messages),
                content=summary_json_text(payload),
            )
            if chunk_callback:
                chunk_callback(1, block)
            return render_summary(
                payload,
                messages,
                source_messages,
                1,
                self.special_member_display_name,
            )

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
                payload = parse_summary_json(
                    saved.content,
                    {message.position for message in chunks[0]},
                    self._special_positions(chunks[0]),
                )
                return render_summary(
                    payload,
                    messages,
                    source_messages,
                    1,
                    self.special_member_display_name,
                )
            payload = self._chat_structured(
                build_summary_prompt(
                    group_name,
                    chunks[0],
                    max_chars=max(self.single_call_max_chars, self.chunk_max_chars),
                    special_member_name=self.special_member_display_name,
                ),
                chunks[0],
            )
            block = SummaryBlock(
                title="完整摘要",
                message_count=len(chunks[0]),
                time_range=_message_time_range(chunks[0]),
                content=summary_json_text(payload),
            )
            if chunk_callback:
                chunk_callback(1, block)
            return render_summary(
                payload,
                messages,
                source_messages,
                1,
                self.special_member_display_name,
            )

        if plan_callback:
            plan_callback(len(chunks))

        def summarize_chunk(item: tuple[int, list[Message]]) -> SummaryBlock:
            index, chunk = item
            chunk_payload = self._chat_structured(
                build_chunk_summary_prompt(
                    group_name,
                    chunk,
                    chunk_index=index,
                    total_chunks=len(chunks),
                    max_chars=self.chunk_max_chars,
                    special_member_name=self.special_member_display_name,
                ),
                chunk,
            )
            block = SummaryBlock(
                title=f"第 {index}/{len(chunks)} 块",
                message_count=len(chunk),
                time_range=_message_time_range(chunk),
                content=summary_json_text(chunk_payload),
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

        payload = self._merge_summary_blocks(
            group_name=group_name,
            blocks=blocks,
            total_message_count=len(messages),
            total_chunk_count=len(chunks),
            messages=messages,
        )
        return render_summary(
            payload,
            messages,
            source_messages,
            len(chunks),
            self.special_member_display_name,
        )

    def _merge_summary_blocks(
        self,
        group_name: str,
        blocks: list[SummaryBlock],
        total_message_count: int,
        total_chunk_count: int,
        messages: list[Message],
    ) -> dict[str, list[dict[str, Any]]]:
        if len(blocks) <= self.merge_max_blocks and len(_summary_blocks_text(blocks)) <= self.merge_max_chars:
            return self._chat_structured(
                build_merge_summary_prompt(
                    group_name=group_name,
                    blocks=blocks,
                    total_message_count=total_message_count,
                    total_chunk_count=total_chunk_count,
                    final=True,
                    max_chars=self.merge_max_chars,
                    special_member_name=self.special_member_display_name,
                ),
                messages,
                source_blocks=blocks,
            )

        batches = split_summary_blocks(
            blocks,
            max_blocks=self.merge_max_blocks,
            max_chars=self.merge_max_chars,
        )
        if len(batches) == 1 or len(batches) == len(blocks):
            return self._chat_structured(
                build_merge_summary_prompt(
                    group_name=group_name,
                    blocks=blocks,
                    total_message_count=total_message_count,
                    total_chunk_count=total_chunk_count,
                    final=True,
                    max_chars=self.merge_max_chars,
                    special_member_name=self.special_member_display_name,
                ),
                messages,
                source_blocks=blocks,
            )

        reduced_blocks: list[SummaryBlock] = []
        for index, batch in enumerate(batches, start=1):
            batch_positions = {
                position
                for block in batch
                for position in self._block_positions(block)
            }
            batch_messages = [message for message in messages if message.position in batch_positions]
            merged = self._chat_structured(
                build_merge_summary_prompt(
                    group_name=group_name,
                    blocks=batch,
                    total_message_count=sum(block.message_count for block in batch),
                    total_chunk_count=total_chunk_count,
                    final=False,
                    max_chars=self.merge_max_chars,
                    special_member_name=self.special_member_display_name,
                ),
                batch_messages,
                source_blocks=batch,
            )
            reduced_blocks.append(
                SummaryBlock(
                    title=f"合并块 {index}/{len(batches)}",
                    message_count=sum(block.message_count for block in batch),
                    time_range=f"{batch[0].time_range}；{batch[-1].time_range}",
                    content=summary_json_text(merged),
                )
            )

        return self._merge_summary_blocks(
            group_name=group_name,
            blocks=reduced_blocks,
            total_message_count=total_message_count,
            total_chunk_count=total_chunk_count,
            messages=messages,
        )

    @staticmethod
    def _special_positions(messages: list[Message]) -> set[int]:
        return {
            message.position
            for message in messages
            if message.is_special_sender or message.mentions_special or message.replies_to_special
        }

    @staticmethod
    def _block_positions(block: SummaryBlock) -> set[int]:
        try:
            payload = json.loads(block.content)
        except json.JSONDecodeError:
            return set()
        return {
            int(position)
            for section in SUMMARY_SECTIONS
            for item in payload.get(section, [])
            if isinstance(item, dict)
            for position in item.get("evidence", [])
        }

    @staticmethod
    def _block_evidence_metadata(
        blocks: list[SummaryBlock],
    ) -> tuple[dict[int, set[str]], set[int]]:
        types_by_position: dict[int, set[str]] = {}
        special_positions: set[int] = set()
        for block in blocks:
            try:
                payload = json.loads(block.content)
            except json.JSONDecodeError:
                continue
            for section in SUMMARY_SECTIONS:
                for item in payload.get(section, []):
                    if not isinstance(item, dict):
                        continue
                    item_type = str(item.get("type") or "unknown")
                    for raw_position in item.get("evidence", []):
                        position = int(raw_position)
                        types_by_position.setdefault(position, set()).add(item_type)
                        if section == "special_member":
                            special_positions.add(position)
        return types_by_position, special_positions

    def _chat_structured(
        self,
        messages: list[dict[str, str]],
        source_messages: list[Message],
        source_blocks: list[SummaryBlock] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        allowed_positions = {message.position for message in source_messages}
        special_positions = self._special_positions(source_messages)
        source_types: dict[int, set[str]] = {}
        if source_blocks is not None:
            source_types, special_positions = self._block_evidence_metadata(source_blocks)
        last_error: SummarizerError | None = None
        prompt = list(messages)
        for attempt in range(2):
            raw = self._chat(prompt)
            try:
                payload = parse_summary_json(raw, allowed_positions, special_positions)
                if source_types:
                    for section in SUMMARY_SECTIONS:
                        for item in payload[section]:
                            input_types = {
                                item_type
                                for position in item["evidence"]
                                for item_type in source_types.get(position, set())
                            }
                            if item["type"] not in input_types:
                                raise SummarizerError(
                                    "Merge changed an item's evidence type"
                                )
                return payload
            except SummarizerError as exc:
                last_error = exc
                if attempt == 0:
                    prompt = [
                        *messages,
                        {
                            "role": "user",
                            "content": (
                                f"上次输出未通过校验：{exc}。"
                                "请精简重复条目，重新输出完整、合法且证据位置正确的 JSON，"
                                "并确保 JSON 在输出上限内完整结束。"
                            ),
                        },
                    ]
        raise last_error or SummarizerError("Unable to validate DeepSeek summary JSON")

    def _chat(self, messages: list[dict[str, str]]) -> str:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": 8192,
            "stream": False,
            "response_format": {"type": "json_object"},
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
        attempts = self.request_retries + 1
        body = ""
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                if exc.code not in {429, 500, 502, 503, 504} or attempt >= attempts - 1:
                    raise SummarizerError(f"DeepSeek API returned HTTP {exc.code}: {detail}") from exc
                last_error = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt >= attempts - 1:
                    elapsed = time.time() - started_at
                    if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                        raise SummarizerError(
                            f"DeepSeek API read timed out after {attempts} attempts ({elapsed:.1f}s)"
                        ) from exc
                    raise SummarizerError(f"DeepSeek API request failed after {attempts} attempts: {exc}") from exc
                last_error = exc
            time.sleep(min(2 ** attempt, 5))
        if not body:
            raise SummarizerError(f"DeepSeek API request failed: {last_error}") from last_error

        try:
            data = json.loads(body)
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            elapsed = time.time() - started_at
            raise SummarizerError(f"Unexpected DeepSeek API response after {elapsed:.1f}s") from exc

        return strip_markdown_noise(str(content))
