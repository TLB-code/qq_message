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
SUMMARY_PIPELINE_VERSION = 3
DEFAULT_SPECIAL_MEMBER_NAME = "魔女公主♪"
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
ACTION_STATES = {"none", "open", "decided", "completed"}
ATTENTION_REASONS = {
    "none",
    "explicit_request",
    "explicit_concern",
    "unresolved_disagreement",
}
NAMED_QQ_MENTION_PATTERN = re.compile(r"@([^()\s]{1,60})\((\d{5,12})\)")
QQ_MENTION_PATTERN = re.compile(r"@(\d{5,12})\b")
MEMBER_ALIAS_PATTERN = re.compile(r"成员\d{3}|重点成员")
ESCALATION_PATTERN = re.compile(r"需要支援|需要回应|尚未解决|未解决|存在风险|需进一步讨论")


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
        "你是谨慎的 QQ 群消息事件提取助手。"
        "只能依据聊天原文提取事件和逐项主张，不得补充常识、动机、风险、诊断或现实身份。"
        "聊天可能同时交错多个话题，不能因为时间相近就把无关消息合并。"
    )
    user = f"""
请总结 QQ 群「{group_name}」的第 {chunk_index}/{total_chunks} 块消息。
本块共 {len(messages)} 条，时间范围：{_message_time_range(messages)}。

只输出一个 JSON 对象，不要使用 Markdown。固定结构：{{"items": [事件]}}。
每个事件固定结构：
{{
  "title": "简短标题",
  "type": "证据类型",
  "action_state": "行动状态",
  "attention_reason": "关注原因",
  "claims": [
    {{"text": "只表达一个结论", "evidence": [直接支持该结论的消息位置]}}
  ]
}}

证据类型只能使用：confirmed_fact、confirmed_plan、proposal、banter、self_report、reported_concern、disagreement、unknown。
行动状态只能使用：none、open、decided、completed。
关注原因只能使用：none、explicit_request、explicit_concern、unresolved_disagreement。
规则：
- 明确发生的事实用 confirmed_fact；明确承诺或未来安排用 confirmed_plan；建议和意向用 proposal。
- 玩笑、跟风、角色扮演、暧昧话术用 banter，绝不能改写成真实计划、纠纷、求助或风险。
- 成员对账号、金额、经历和健康的自述用 self_report，不得写成已核实事实。
- 别人表达担忧用 reported_concern，不得升级为客观健康风险或诊断。
- action_state=open 只用于有执行人且尚待执行的未来动作；decided 用于明确决定；已回答的问题不是待办。
- attention_reason 只有原文明确请求处理、明确表达担忧或明确存在未决分歧时才能非 none。
- type=banter 时 action_state 和 attention_reason 必须都是 none。
- 不得自行写“没人回应”“尚未解决”“需要支援”“存在风险”“需要进一步讨论”。
- 昵称可能是角色扮演，不得映射到现实同名人物；不得改变原词含义，例如“黄油”不能写成“黄图”。
- “成员009”是系统别名，与昵称文字“009”完全不同；涉及成员时只能使用消息行方括号中的成员别名。
- 不要输出 participants，参与者由程序根据每条 claim 的证据发送者生成。
- 每条 claim 必须只引用直接支持自身文字的 1-5 条消息，不要加入仅仅时间相近的其他话题。
- 看不到图片、转发和文件的实际内容，只能写群友文字中明确表达的信息。
- 带“重点成员本人”“@重点成员”或“回复重点成员”的消息需要作为独立事件保留；“{special_member_name}”只是显示名。
- 合并重复刷屏，但尽量覆盖有实际信息的独立话题、明确个人计划和群体决定。

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
        "你是 QQ 群结构化事件合并助手。"
        "只能合并输入 JSON 已有的事件与主张，并保留原始证据位置。"
        "不能添加事实，也不能把无关的并发对话合成一个事件。"
    )
    task = "请合并为最终结构" if final else "请合并为中间结构"
    output = f"""
只输出 JSON，不要输出 Markdown。输出结构与输入完全相同：一个 items 数组。
- 合并同一事件时保留 claims 与各自 evidence；不同结论不能共用不相关证据。
- type 只能是：{', '.join(sorted(EVIDENCE_TYPES))}。
- confirmed_plan、proposal、banter、self_report、reported_concern、disagreement 不能在合并时升级为 confirmed_fact。
- banter 不能改成行动、关注、纠纷或风险。
- action_state、attention_reason 只能沿用输入已有状态或降级为 none，不得自行升级。
- 不要输出参与者、状态描述、总览或 Markdown。
- 重点成员身份只能由原事件的证据标记继承，不得根据昵称识别。“{special_member_name}”仅为显示名。
- 每个 claim 保留 1-5 个最直接证据；删除重复事件，但不要漏掉跨多个分块持续发生的主要事件。
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


def build_review_summary_prompt(
    group_name: str,
    payload: dict[str, list[dict[str, Any]]],
    messages: list[Message],
) -> list[dict[str, str]]:
    positions = {
        position
        for item in payload["items"]
        for claim in item["claims"]
        for position in claim["evidence"]
    }
    evidence_messages = [message for message in messages if message.position in positions]
    system = (
        "你是保守的摘要证据复核员。"
        "只能删减、降级或纠正候选事件，不能加入候选中不存在的新事件、新结论或新证据。"
        "证据不能直接支持主张时必须删除该主张；玩笑必须降级为 banter。"
    )
    user = f"""
请复核 QQ 群「{group_name}」的候选事件。
只输出与候选相同的 JSON 结构：{{"items": [...]}}。

复核规则：
- 每条 claim 的每个关键结论必须能从它自己的 evidence 原文直接读出。
- 昵称“009”不能解释成系统别名“成员009”；只能相信消息行方括号中的发送者别名。
- 不得把“赌狗”归给没有说这个词的人，不得把“太成熟/太老”改写成“要求换图”。
- 游戏互怼、“杂鱼”“可恶”“退出某社”等语境默认是 banter，除非原文明示真实求助或担忧。
- “需要回应、未解决、需要支援、存在风险”必须有原文直接依据，否则 attention_reason=none 并删除相关措辞。
- 已经在证据中得到回答的问题不能是 open 行动。
- 不得增加候选 evidence 之外的位置。允许删除无关证据、删除主张、拆分混杂事件或删除整个事件。
- 不要输出 participants、Markdown 或解释。

候选事件：
{summary_json_text(payload)}

候选事件引用的原消息：
{compact_messages(evidence_messages, max_chars=MERGE_MAX_CHARS)}
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


def _decode_json_object(value: str) -> dict[str, Any]:
    text = value.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        object_start = text.find("{")
        if object_start < 0:
            raise SummarizerError("DeepSeek did not return valid summary JSON") from exc
        try:
            payload, _ = json.JSONDecoder().raw_decode(text[object_start:])
        except json.JSONDecodeError as nested_exc:
            raise SummarizerError("DeepSeek did not return valid summary JSON") from nested_exc
    if not isinstance(payload, dict):
        raise SummarizerError("DeepSeek summary JSON must be an object")
    return payload


def _parse_evidence_position(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not an evidence position")
    if isinstance(value, str):
        value = value.strip()
        if value.startswith("#"):
            value = value[1:].strip()
    return int(value)


def parse_summary_json(
    value: str,
    allowed_positions: set[int],
    special_positions: set[int],
) -> dict[str, list[dict[str, Any]]]:
    payload = _decode_json_object(value)
    raw_items = payload.get("items", [])
    if not isinstance(raw_items, list):
        raise SummarizerError("Summary items must be an array")

    items: list[dict[str, Any]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise SummarizerError("Summary contains a non-object item")
        item_type = str(raw_item.get("type") or "unknown").strip()
        action_state = str(raw_item.get("action_state") or "none").strip()
        attention_reason = str(raw_item.get("attention_reason") or "none").strip()
        if item_type not in EVIDENCE_TYPES:
            raise SummarizerError(f"Unsupported evidence type: {item_type}")
        if action_state not in ACTION_STATES:
            raise SummarizerError(f"Unsupported action state: {action_state}")
        if attention_reason not in ATTENTION_REASONS:
            raise SummarizerError(f"Unsupported attention reason: {attention_reason}")
        if item_type == "confirmed_plan" and action_state == "none":
            action_state = "open"
        if item_type == "banter" and (action_state != "none" or attention_reason != "none"):
            raise SummarizerError("Banter cannot be an action or attention item")
        if item_type == "banter" and re.search(r"纠纷|求救|支援|风险|未解决", str(raw_item.get("title") or "")):
            raise SummarizerError("Banter title cannot promote a joke into a real problem")
        if action_state == "open" and item_type not in {
            "confirmed_fact",
            "confirmed_plan",
            "proposal",
            "self_report",
        }:
            raise SummarizerError("Open action has an incompatible evidence type")
        if action_state in {"decided", "completed"} and item_type not in {
            "confirmed_fact",
            "confirmed_plan",
            "proposal",
            "self_report",
        }:
            raise SummarizerError("Decided or completed action has an incompatible evidence type")
        if attention_reason == "explicit_concern" and item_type != "reported_concern":
            raise SummarizerError("Explicit concern requires reported_concern type")
        if item_type == "reported_concern" and attention_reason != "explicit_concern":
            raise SummarizerError("Reported concern requires an explicit concern reason")
        if attention_reason == "unresolved_disagreement" and item_type != "disagreement":
            raise SummarizerError("Unresolved disagreement requires disagreement type")

        title = str(raw_item.get("title") or "").strip()
        raw_claims = raw_item.get("claims")
        if not title or not isinstance(raw_claims, list) or not raw_claims:
            raise SummarizerError("Every summary item needs a title and claims")
        claims = []
        item_positions: list[int] = []
        for raw_claim in raw_claims[:6]:
            if not isinstance(raw_claim, dict):
                raise SummarizerError("Summary claim must be an object")
            claim_text = str(raw_claim.get("text") or "").strip()
            evidence_raw = raw_claim.get("evidence")
            if not claim_text or not isinstance(evidence_raw, list) or not evidence_raw:
                raise SummarizerError("Every claim needs text and evidence")
            try:
                evidence = list(
                    dict.fromkeys(_parse_evidence_position(position) for position in evidence_raw)
                )[:5]
            except (TypeError, ValueError) as exc:
                raise SummarizerError("Evidence positions must be integers") from exc
            if any(position not in allowed_positions for position in evidence):
                raise SummarizerError("Summary referenced a message outside the provided input")
            if attention_reason == "none" and ESCALATION_PATTERN.search(claim_text):
                raise SummarizerError("Claim added an unsupported attention conclusion")
            claims.append({"text": claim_text, "evidence": evidence})
            item_positions.extend(evidence)
        evidence = list(dict.fromkeys(item_positions))
        special_evidence = [position for position in evidence if position in special_positions]
        items.append(
            {
                "title": title,
                "type": item_type,
                "action_state": action_state,
                "attention_reason": attention_reason,
                "special_related": bool(special_evidence),
                "special_evidence": special_evidence,
                "claims": claims,
                "evidence": evidence,
            }
        )
    return {"items": items}


def summary_json_text(payload: dict[str, list[dict[str, Any]]]) -> str:
    serializable = {
        "items": [
            {
                "title": item["title"],
                "type": item["type"],
                "action_state": item["action_state"],
                "attention_reason": item["attention_reason"],
                "special_related": item["special_related"],
                "special_evidence": item["special_evidence"],
                "claims": item["claims"],
            }
            for item in payload["items"]
        ]
    }
    return json.dumps(serializable, ensure_ascii=False, separators=(",", ":"))


def _display_names(messages: list[Message], special_member_name: str) -> dict[str, str]:
    latest_names: dict[str, str] = {}
    for message in messages:
        if message.sender_alias:
            latest_names[message.sender_alias] = message.sender_name
    name_counts: dict[str, int] = {}
    for name in latest_names.values():
        name_counts[name] = name_counts.get(name, 0) + 1
    return {
        alias: (
            special_member_name
            if alias == "重点成员"
            else f"{name}（{alias}）"
            if name_counts[name] > 1
            else name
        )
        for alias, name in latest_names.items()
    }


def _replace_aliases(text: str, display_names: dict[str, str]) -> str:
    return MEMBER_ALIAS_PATTERN.sub(lambda match: display_names.get(match.group(0), match.group(0)), text)


def _item_participants(
    item: dict[str, Any],
    messages_by_position: dict[int, Message],
) -> list[str]:
    aliases = []
    for position in item["evidence"]:
        message = messages_by_position.get(position)
        if message and message.sender_alias and message.sender_alias not in aliases:
            aliases.append(message.sender_alias)
    return aliases[:12]


def _merge_duplicate_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for item in items:
        evidence = set(item["evidence"])
        duplicate = next(
            (
                existing
                for existing in merged
                if existing["type"] == item["type"]
                and evidence
                and set(existing["evidence"])
                and len(evidence & set(existing["evidence"]))
                / min(len(evidence), len(set(existing["evidence"])))
                >= 0.8
            ),
            None,
        )
        if duplicate is None:
            merged.append(item)
            continue
        seen = {(claim["text"], tuple(claim["evidence"])) for claim in duplicate["claims"]}
        duplicate["claims"].extend(
            claim
            for claim in item["claims"]
            if (claim["text"], tuple(claim["evidence"])) not in seen
        )
        duplicate["evidence"] = list(dict.fromkeys([*duplicate["evidence"], *item["evidence"]]))
        duplicate["special_related"] = duplicate["special_related"] or item["special_related"]
        duplicate["special_evidence"] = list(
            dict.fromkeys(
                [*duplicate.get("special_evidence", []), *item.get("special_evidence", [])]
            )
        )
    return merged


def _item_score(
    item: dict[str, Any],
    messages_by_position: dict[int, Message],
) -> float:
    evidence_messages = [messages_by_position[p] for p in item["evidence"] if p in messages_by_position]
    participants = {message.user_id for message in evidence_messages}
    duration = 0
    if len(evidence_messages) > 1:
        duration = max(message.timestamp for message in evidence_messages) - min(
            message.timestamp for message in evidence_messages
        )
    score = len(item["evidence"]) + min(len(participants), 6) * 1.5 + min(duration / 60, 6)
    if item["action_state"] in {"open", "decided"}:
        score += 3
    if item["attention_reason"] != "none":
        score += 2
    if item["type"] == "banter":
        score -= 3
    return score


def _evidence_label(positions: list[int], messages_by_position: dict[int, Message]) -> str:
    labels = []
    for position in positions:
        message = messages_by_position.get(position)
        if message is not None:
            labels.append(f"#{position} {_format_time(message.timestamp)}")
    return "、".join(labels)


def _render_items(
    items: list[dict[str, Any]],
    messages_by_position: dict[int, Message],
    display_names: dict[str, str],
    empty_text: str,
) -> list[str]:
    if not items:
        return [empty_text]
    type_labels = {
        "confirmed_fact": "已确认事实",
        "confirmed_plan": "明确安排",
        "proposal": "提议/意向",
        "banter": "玩笑互动",
        "self_report": "成员自述",
        "reported_concern": "群友担忧",
        "disagreement": "存在分歧",
        "unknown": "原文信息",
    }
    action_labels = {"open": "待执行", "decided": "已决定", "completed": "已完成"}
    attention_labels = {
        "explicit_request": "明确请求",
        "explicit_concern": "明确担忧",
        "unresolved_disagreement": "未决分歧",
    }
    lines = []
    for item in items:
        details = [type_labels[item["type"]]]
        if item["action_state"] != "none":
            details.append(action_labels[item["action_state"]])
        if item["attention_reason"] != "none":
            details.append(attention_labels[item["attention_reason"]])
        aliases = _item_participants(item, messages_by_position)
        if aliases:
            details.append(f"参与者：{'、'.join(display_names.get(alias, alias) for alias in aliases)}")
        claims = []
        for claim in item["claims"]:
            claim_text = _replace_aliases(claim["text"], display_names)
            evidence = _evidence_label(claim["evidence"], messages_by_position)
            claims.append(claim_text + (f" [证据：{evidence}]" if evidence else ""))
        title = _replace_aliases(item["title"], display_names)
        lines.append(f"**{title}**（{'；'.join(details)}）：{'；'.join(claims)}")
    return lines


def render_summary(
    payload: dict[str, list[dict[str, Any]]],
    summary_messages: list[Message],
    source_messages: list[Message],
    total_chunk_count: int,
    special_member_name: str,
) -> str:
    messages_by_position = {message.position: message for message in summary_messages}
    display_names = _display_names(summary_messages, special_member_name)
    items = _merge_duplicate_items([dict(item) for item in payload["items"]])
    attention_items = [item for item in items if item["attention_reason"] != "none"]
    action_items = [
        item
        for item in items
        if item["attention_reason"] == "none" and item["action_state"] in {"open", "decided"}
    ]
    topic_items = [
        item
        for item in items
        if item not in attention_items
        and item not in action_items
        and not (
            item["special_related"]
            and item["type"] == "banter"
            and len(item["evidence"]) <= 3
        )
    ]
    special_items = [item for item in items if item["special_related"]]
    overview_items = sorted(
        items,
        key=lambda item: _item_score(item, messages_by_position),
        reverse=True,
    )[:6]

    lines = ["## 总览"]
    if overview_items:
        for item in overview_items:
            overview_text = "；".join(
                _replace_aliases(claim["text"], display_names) for claim in item["claims"]
            )
            title = _replace_aliases(item["title"], display_names)
            lines.append(f"- **{title}**：{overview_text}")
    else:
        lines.append("- 本批消息没有可确认的文本话题。")
    lines.extend(["", "## 重点话题"])
    lines.extend(_render_items(topic_items, messages_by_position, display_names, "暂无明确重点话题。"))
    lines.extend(["", "## 待办/决定"])
    lines.extend(_render_items(action_items, messages_by_position, display_names, "暂无明确待办。"))
    lines.extend(["", "## 需要关注"])
    lines.extend(_render_items(attention_items, messages_by_position, display_names, "暂无特别需要关注。"))
    lines.extend(["", f"## {special_member_name} 专属"])
    lines.extend(
        _render_items(
            special_items,
            messages_by_position,
            display_names,
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
                payload = self._review_payload(group_name, payload, messages, stage_callback)
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
            payload = self._review_payload(group_name, payload, messages, stage_callback)
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
                payload = self._review_payload(group_name, payload, messages, stage_callback)
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
            payload = self._review_payload(group_name, payload, messages, stage_callback)
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
        payload = self._review_payload(group_name, payload, messages, stage_callback)
        return render_summary(
            payload,
            messages,
            source_messages,
            len(chunks),
            self.special_member_display_name,
        )

    def _review_payload(
        self,
        group_name: str,
        payload: dict[str, list[dict[str, Any]]],
        messages: list[Message],
        stage_callback: Callable[[str], None] | None,
    ) -> dict[str, list[dict[str, Any]]]:
        if not payload["items"]:
            return payload
        if stage_callback:
            stage_callback("reviewing")
        allowed_positions = {
            position
            for item in payload["items"]
            for claim in item["claims"]
            for position in claim["evidence"]
        }
        evidence_messages = [message for message in messages if message.position in allowed_positions]
        reviewed = self._chat_structured(
            build_review_summary_prompt(group_name, payload, messages),
            evidence_messages,
        )
        original_evidence_sets = [set(item["evidence"]) for item in payload["items"]]
        for item in reviewed["items"]:
            if not any(set(item["evidence"]).issubset(source) for source in original_evidence_sets):
                raise SummarizerError("Evidence review mixed evidence from different candidate events")
        return reviewed

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
            for item in payload.get("items", [])
            if isinstance(item, dict)
            for claim in item.get("claims", [])
            if isinstance(claim, dict)
            for position in claim.get("evidence", [])
        }

    @staticmethod
    def _block_evidence_metadata(
        blocks: list[SummaryBlock],
    ) -> tuple[
        dict[int, set[str]],
        dict[int, set[str]],
        dict[int, set[str]],
        set[int],
    ]:
        types_by_position: dict[int, set[str]] = {}
        actions_by_position: dict[int, set[str]] = {}
        attention_by_position: dict[int, set[str]] = {}
        special_positions: set[int] = set()
        for block in blocks:
            try:
                payload = json.loads(block.content)
            except json.JSONDecodeError:
                continue
            for item in payload.get("items", []):
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "unknown")
                action_state = str(item.get("action_state") or "none")
                attention_reason = str(item.get("attention_reason") or "none")
                for claim in item.get("claims", []):
                    if not isinstance(claim, dict):
                        continue
                    for raw_position in claim.get("evidence", []):
                        position = int(raw_position)
                        types_by_position.setdefault(position, set()).add(item_type)
                        actions_by_position.setdefault(position, set()).add(action_state)
                        attention_by_position.setdefault(position, set()).add(attention_reason)
                special_positions.update(int(position) for position in item.get("special_evidence", []))
        return types_by_position, actions_by_position, attention_by_position, special_positions

    @staticmethod
    def _validate_claim_aliases(
        payload: dict[str, list[dict[str, Any]]],
        messages: list[Message],
    ) -> None:
        messages_by_position = {message.position: message for message in messages}
        for item in payload["items"]:
            item_aliases = set(MEMBER_ALIAS_PATTERN.findall(item["title"]))
            item_allowed_aliases = set()
            for position in item["evidence"]:
                message = messages_by_position.get(position)
                if message is None:
                    continue
                if message.sender_alias:
                    item_allowed_aliases.add(message.sender_alias)
                item_allowed_aliases.update(MEMBER_ALIAS_PATTERN.findall(message.content))
            if not item_aliases.issubset(item_allowed_aliases):
                unsupported = sorted(item_aliases - item_allowed_aliases)
                raise SummarizerError(
                    "Title aliases "
                    f"{unsupported} are outside event evidence aliases {sorted(item_allowed_aliases)}"
                )
            for claim in item["claims"]:
                referenced_aliases = set(MEMBER_ALIAS_PATTERN.findall(claim["text"]))
                if not referenced_aliases.issubset(item_allowed_aliases):
                    unsupported = sorted(referenced_aliases - item_allowed_aliases)
                    raise SummarizerError(
                        "Claim aliases "
                        f"{unsupported} are outside event evidence aliases {sorted(item_allowed_aliases)}"
                    )

    def _chat_structured(
        self,
        messages: list[dict[str, str]],
        source_messages: list[Message],
        source_blocks: list[SummaryBlock] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        allowed_positions = {message.position for message in source_messages}
        special_positions = self._special_positions(source_messages)
        source_types: dict[int, set[str]] = {}
        source_actions: dict[int, set[str]] = {}
        source_attention: dict[int, set[str]] = {}
        if source_blocks is not None:
            (
                source_types,
                source_actions,
                source_attention,
                special_positions,
            ) = self._block_evidence_metadata(source_blocks)
        last_error: SummarizerError | None = None
        prompt = list(messages)
        for attempt in range(2):
            raw = self._chat(prompt)
            try:
                payload = parse_summary_json(raw, allowed_positions, special_positions)
                self._validate_claim_aliases(payload, source_messages)
                if source_types:
                    for item in payload["items"]:
                        if any(
                            item["type"] not in source_types.get(position, set())
                            for position in item["evidence"]
                        ):
                            raise SummarizerError("Merge changed an item's evidence type")
                        if item["action_state"] != "none":
                            input_actions = {
                                state
                                for position in item["evidence"]
                                for state in source_actions.get(position, set())
                            }
                            if item["action_state"] not in input_actions:
                                raise SummarizerError("Merge promoted an item's action state")
                        if item["attention_reason"] != "none":
                            input_attention = {
                                reason
                                for position in item["evidence"]
                                for reason in source_attention.get(position, set())
                            }
                            if item["attention_reason"] not in input_attention:
                                raise SummarizerError("Merge promoted an item's attention reason")
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
