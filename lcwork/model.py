"""信用证交单领域对象与资料仓储。

数据来源是 ``samples/`` 下的四份 JSONL 交换资料，本模块只负责把行记录
变成带索引的领域对象，不做业务判定。业务规则见 :mod:`lcwork.engine`。
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional


KNOWN_EVENTS = frozenset(
    {
        "received",
        "discrepancy",
        "waiver",
        "paid",
        "amount_released",
        "originals_returned",
    }
)


class FeedError(ValueError):
    """交换资料无法支持业务解释时抛出，消息中指明涉及的记录编号。"""


@dataclass(frozen=True)
class CreditRev:
    """信用证的一个修订版（revision 1 为原证）。"""

    credit: str
    revision: int
    currency: str
    limit_minor: int
    partial: bool
    latest_ship: date
    latest_present: date
    models: tuple[str, ...]
    buyer_ok: bool
    seller_ok: bool
    bank_effective: str

    @property
    def effective(self) -> bool:
        """买卖双方同意且收到开证行生效通知，才可用于新的交单。"""
        return self.buyer_ok and self.seller_ok and bool(self.bank_effective)

    @property
    def missing_effectiveness(self) -> list[str]:
        """逐条列出还差哪一份同意/通知，便于直接答复“缺什么授权”。"""
        missing: list[str] = []
        if not self.buyer_ok:
            missing.append("买方未同意修订")
        if not self.seller_ok:
            missing.append("卖方未同意修订")
        if not self.bank_effective:
            missing.append("缺开证行生效通知")
        return missing

    @property
    def ref(self) -> str:
        return f"{self.credit}@rev{self.revision}"


@dataclass(frozen=True)
class BillRev:
    """船代按版签发的提单；同一 ``original_group`` 代表同一套正本。"""

    bill: str
    revision: int
    original_group: str
    holder: str
    voyage: str
    shipped: date
    vehicles: tuple[tuple[str, str], ...]  # (vin, model)

    @property
    def models(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(model for _, model in self.vehicles))

    @property
    def vins(self) -> tuple[str, ...]:
        return tuple(vin for vin, _ in self.vehicles)

    @property
    def ref(self) -> str:
        return f"{self.bill}@rev{self.revision}"


@dataclass(frozen=True)
class BankMessage:
    """银行进件消息，``seq`` 是资料交付顺序（文件行序）。"""

    message: str
    presentation: str
    credit: str
    revision: int
    event: str
    minor: int
    fee_minor: int
    original_group: str
    discrepancies: tuple[str, ...]
    seq: int


@dataclass(frozen=True)
class Permission:
    """持有人就某个正本组对具体交单人的单次提交授权。"""

    approval: str
    group: str
    holder: str
    presenter: str
    at: datetime


class Repository:
    """只读仓储：构造索引并提供业务层需要的查找方法。"""

    def __init__(
        self,
        credits: list[CreditRev],
        bills: list[BillRev],
        messages: list[BankMessage],
        permissions: list[Permission],
    ) -> None:
        self._credit_rev = {(c.credit, c.revision): c for c in credits}
        self._credits: dict[str, list[CreditRev]] = {}
        for rev in credits:
            self._credits.setdefault(rev.credit, []).append(rev)
        for revs in self._credits.values():
            revs.sort(key=lambda r: r.revision)

        self._bill_rev = {(b.bill, b.revision): b for b in bills}
        self._groups: dict[str, list[BillRev]] = {}
        for bill in bills:
            self._groups.setdefault(bill.original_group, []).append(bill)
        for revs in self._groups.values():
            revs.sort(key=lambda b: (b.revision, b.bill))

        self._messages = sorted(messages, key=lambda m: m.seq)
        self._by_presentation: dict[str, list[BankMessage]] = {}
        for msg in self._messages:
            self._by_presentation.setdefault(msg.presentation, []).append(msg)

        self._permissions = sorted(permissions, key=lambda p: p.at)
        self._perms_by_group: dict[str, list[Permission]] = {}
        for perm in self._permissions:
            self._perms_by_group.setdefault(perm.group, []).append(perm)

    # -- 信用证 -------------------------------------------------------
    def credit_revisions(self, credit: str) -> list[CreditRev]:
        return list(self._credits.get(credit, ()))

    def credit_rev(self, credit: str, revision: int) -> Optional[CreditRev]:
        return self._credit_rev.get((credit, revision))

    def latest_effective_rev(self, credit: str) -> Optional[CreditRev]:
        effective = [r for r in self._credits.get(credit, ()) if r.effective]
        return effective[-1] if effective else None

    def all_credits(self) -> list[str]:
        return sorted(self._credits)

    # -- 提单/正本 ----------------------------------------------------
    def group_revisions(self, group: str) -> list[BillRev]:
        return list(self._groups.get(group, ()))

    def latest_group_rev(self, group: str) -> Optional[BillRev]:
        revs = self._groups.get(group)
        return revs[-1] if revs else None

    # -- 银行消息 -----------------------------------------------------
    @property
    def messages(self) -> list[BankMessage]:
        return list(self._messages)

    def presentation_ids(self) -> list[str]:
        ordered = sorted(
            self._by_presentation,
            key=lambda pr: min(m.seq for m in self._by_presentation[pr]),
        )
        return ordered

    def messages_of(self, presentation: str) -> list[BankMessage]:
        return list(self._by_presentation.get(presentation, ()))

    # -- 授权 ---------------------------------------------------------
    def permissions_of_group(self, group: str) -> list[Permission]:
        return list(self._perms_by_group.get(group, ()))
