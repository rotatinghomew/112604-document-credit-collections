"""把项目约定的 JSONL 交换资料装载为 :mod:`lcwork.model` 领域对象。

进件采用严格模式：字段、引用、金额、消息顺序或 waiver 范围不符合
``samples/formats.json`` 约定时直接抛 :class:`FeedError`，避免脏数据
被悄悄解释成业务结论。
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path

from .model import (
    BankMessage,
    BillRev,
    CreditRev,
    FeedError,
    KNOWN_EVENTS,
    Permission,
    Repository,
)

_FIELD_TYPES = {
    "string": str,
    "int": int,
    "bool": bool,
    "list": list,
    "time": str,
    "date": str,
}
_FEED_NAMES = ("credits", "bills", "bank_messages", "bilateral_permissions")


def _parse_utc(value: str, where: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FeedError(f"{where} 时间 {value!r} 不是 ISO 8601") from exc
    if parsed.tzinfo is not timezone.utc or not value.endswith("Z"):
        raise FeedError(f"{where} 时间 {value!r} 必须是以 Z 结尾的 UTC 时间")
    return parsed


def _parse_day(value: str, where: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise FeedError(f"{where} 日期 {value!r} 不是 ISO 日期") from exc
    if parsed.isoformat() != value:
        raise FeedError(f"{where} 日期 {value!r} 必须写成 YYYY-MM-DD")
    return parsed


def _check_shape(row: dict, spec: dict, where: str) -> None:
    if set(row) != set(spec["fields"]):
        raise FeedError(f"{where} 字段集合与 formats.json 不符：{sorted(row)}")
    for field, kind in spec["fields"].items():
        value = row[field]
        if type(value) is not _FIELD_TYPES[kind]:
            raise FeedError(f"{where} 字段 {field} 类型应为 {kind}")
    if not all(row[key] != "" for key in spec["key"]):
        raise FeedError(f"{where} 主键字段不允许为空字符串")


def load_repository(samples_dir: str | Path) -> Repository:
    """从交换资料目录构造仓储，并完成引用与进件顺序校验。"""
    samples_dir = Path(samples_dir)
    try:
        schema = json.loads((samples_dir / "formats.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FeedError(f"资料目录缺少 formats.json：{samples_dir}") from exc

    rows_by_feed: dict[str, list[dict]] = {}
    for name in _FEED_NAMES:
        path = samples_dir / f"{name}.jsonl"
        if not path.exists():
            raise FeedError(f"缺少交换资料 {path.name}")
        spec = schema["feeds"][name]
        rows: list[dict] = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            where = f"{name}.jsonl 第{line_no}行"
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise FeedError(f"{where} 不是合法 JSON") from exc
            _check_shape(row, spec, where)
            rows.append(row)
        if not rows:
            raise FeedError(f"{name}.jsonl 不允许为空")
        keys = [tuple(row[key] for key in spec["key"]) for row in rows]
        if len(keys) != len(set(keys)):
            raise FeedError(f"{name}.jsonl 存在重复主键 {spec['key']}")
        rows_by_feed[name] = rows

    credits = [_load_credit(row) for row in rows_by_feed["credits"]]
    bills = [_load_bill(row) for row in rows_by_feed["bills"]]
    permissions = [_load_permission(row) for row in rows_by_feed["bilateral_permissions"]]
    messages = [
        _load_message(row, seq) for seq, row in enumerate(rows_by_feed["bank_messages"], 1)
    ]
    _validate_references(credits, bills, messages, permissions)
    _validate_message_intake(messages)
    return Repository(credits, bills, messages, permissions)


def _load_credit(row: dict) -> CreditRev:
    where = f"信用证 {row['credit']} rev{row['revision']}"
    if row["limit_minor"] <= 0:
        raise FeedError(f"{where} 金额必须为正数")
    return CreditRev(
        credit=row["credit"],
        revision=row["revision"],
        currency=row["currency"],
        limit_minor=row["limit_minor"],
        partial=row["partial"],
        latest_ship=_parse_day(row["latest_ship"], where),
        latest_present=_parse_day(row["latest_present"], where),
        models=tuple(row["models"]),
        buyer_ok=row["buyer_ok"],
        seller_ok=row["seller_ok"],
        bank_effective=row["bank_effective"],
    )


def _load_bill(row: dict) -> BillRev:
    where = f"提单 {row['bill']} rev{row['revision']}"
    vehicles: list[tuple[str, str]] = []
    for item in row["vehicles"]:
        if set(item) != {"vin", "model"} or not item["vin"] or not item["model"]:
            raise FeedError(f"{where} vehicles 每件必须含非空 vin 与 model")
        vehicles.append((item["vin"], item["model"]))
    vins = [vin for vin, _ in vehicles]
    if len(vins) != len(set(vins)):
        raise FeedError(f"{where} 同一版提单内 VIN 重复")
    return BillRev(
        bill=row["bill"],
        revision=row["revision"],
        original_group=row["original_group"],
        holder=row["holder"],
        voyage=row["voyage"],
        shipped=_parse_day(row["shipped"], where),
        vehicles=tuple(vehicles),
    )


def _load_permission(row: dict) -> Permission:
    return Permission(
        approval=row["approval"],
        group=row["group"],
        holder=row["holder"],
        presenter=row["presenter"],
        at=_parse_utc(row["at"], f"授权 {row['approval']}"),
    )


def _load_message(row: dict, seq: int) -> BankMessage:
    where = f"银行消息 {row['message']}"
    if row["event"] not in KNOWN_EVENTS:
        raise FeedError(f"{where} 事件 {row['event']} 不在约定范围内")
    if row["minor"] < 0 or row["fee_minor"] < 0:
        raise FeedError(f"{where} 金额与银行费不允许为负")
    return BankMessage(
        message=row["message"],
        presentation=row["presentation"],
        credit=row["credit"],
        revision=row["revision"],
        event=row["event"],
        minor=row["minor"],
        fee_minor=row["fee_minor"],
        original_group=row["original_group"],
        discrepancies=tuple(row["discrepancies"]),
        seq=seq,
    )


def _validate_references(
    credits: list[CreditRev],
    bills: list[BillRev],
    messages: list[BankMessage],
    permissions: list[Permission],
) -> None:
    credit_keys = {(c.credit, c.revision) for c in credits}
    group_holders = {(b.original_group, b.holder) for b in bills}
    groups = {b.original_group for b in bills}

    for msg in messages:
        where = f"银行消息 {msg.message}"
        if (msg.credit, msg.revision) not in credit_keys:
            raise FeedError(f"{where} 引用的信用证版本不存在")
        if msg.original_group not in groups:
            raise FeedError(f"{where} 引用的正本组不存在")

    for perm in permissions:
        where = f"授权 {perm.approval}"
        if (perm.group, perm.holder) not in group_holders:
            raise FeedError(f"{where} 引用的正本组/持有人在提单资料中不存在")

    # 同一 bill 的不同版本必须共用正本组，且 VIN 集合只能更正不能凭空换车。
    by_bill: dict[str, list[BillRev]] = {}
    for bill in bills:
        by_bill.setdefault(bill.bill, []).append(bill)
    for bill_id, revs in by_bill.items():
        revs.sort(key=lambda b: b.revision)
        group = revs[0].original_group
        for rev in revs:
            if rev.original_group != group:
                raise FeedError(f"提单 {bill_id} 各版本必须沿用同一正本组 {group}")


def _validate_message_intake(messages: list[BankMessage]) -> None:
    """按 presentation 校验银行进件顺序与金额、waiver 范围。"""
    grouped: dict[str, list[BankMessage]] = {}
    for msg in messages:
        grouped.setdefault(msg.presentation, []).append(msg)

    for presentation, chain in grouped.items():
        where = f"交单 {presentation}"
        chain = sorted(chain, key=lambda m: m.seq)
        events = [m.event for m in chain]
        if events[0] != "received":
            raise FeedError(f"{where} 第一条银行消息必须是 received")
        if events.count("received") != 1:
            raise FeedError(f"{where} 只能有一条 received 消息")

        groups = {m.original_group for m in chain}
        refs = {(m.credit, m.revision) for m in chain}
        if len(groups) != 1 or len(refs) != 1:
            raise FeedError(f"{where} 的正本组与信用证版本必须全程一致")

        received = next(m for m in chain if m.event == "received")
        settled = sum(
            m.minor for m in chain if m.event in {"paid", "amount_released"}
        )
        if settled > received.minor:
            raise FeedError(f"{where} 议付/释放金额合计超过票面金额")

        listed: set[str] = set()
        for msg in chain:
            if msg.event == "discrepancy":
                listed.update(msg.discrepancies)
            if msg.event == "waiver":
                unknown = set(msg.discrepancies) - listed
                if unknown:
                    raise FeedError(
                        f"{where} 消息 {msg.message} 的 waiver 覆盖了未列明的不符点 {sorted(unknown)}"
                    )
        if any(m.event == "paid" for m in chain) and any(
            m.event == "originals_returned" for m in chain
        ):
            paid_seq = min(m.seq for m in chain if m.event == "paid")
            ret_seq = min(m.seq for m in chain if m.event == "originals_returned")
            if ret_seq < paid_seq:
                raise FeedError(f"{where} 退正本消息不可能早于议付")
