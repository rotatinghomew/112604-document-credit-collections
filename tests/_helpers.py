"""测试共用：直接用领域对象在内存中构造仓储，避免触碰磁盘资料。"""

import sys
from pathlib import Path

# 兼容 python -I（隔离模式）：隔离模式下 cwd 与 PYTHONPATH 都不在 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from datetime import date, datetime, timezone  # noqa: E402

from lcwork.model import BankMessage, BillRev, Permission, Repository, CreditRev  # noqa: E402


def credit(
    cid="LC-1",
    revision=1,
    limit=100_000_000,
    partial=True,
    ship="2026-12-10",
    present="2026-12-20",
    models=("M1",),
    buyer_ok=True,
    seller_ok=True,
    bank_effective="B-1",
    currency="USD",
):
    return CreditRev(
        cid, revision, currency, limit, partial, date.fromisoformat(ship),
        date.fromisoformat(present), tuple(models), buyer_ok, seller_ok, bank_effective,
    )


def bill(
    bid="BL-1",
    revision=1,
    group="G-1",
    holder="FWD-1",
    voyage="VO-1",
    shipped="2026-12-01",
    vehicles=(("V1", "M1"),),
):
    return BillRev(
        bid, revision, group, holder, voyage, date.fromisoformat(shipped), tuple(vehicles)
    )


def perm(approval="A-1", group="G-1", holder="FWD-1", presenter="EX-1", at="2026-12-02T00:00:00Z"):
    return Permission(
        approval, group, holder, presenter, datetime.fromisoformat(at).replace(tzinfo=timezone.utc)
    )


def msg(
    message,
    presentation,
    event,
    minor=0,
    fee=0,
    group="G-1",
    credit="LC-1",
    revision=1,
    discrepancies=(),
):
    return BankMessage(
        message, presentation, credit, revision, event, minor, fee,
        group, tuple(discrepancies), seq=0,
    )


def build_repo(credits=(), bills=(), messages=(), perms=()):
    numbered = [
        BankMessage(
            m.message, m.presentation, m.credit, m.revision, m.event,
            m.minor, m.fee_minor, m.original_group, m.discrepancies, seq=i,
        )
        for i, m in enumerate(messages, 1)
    ]
    return Repository(list(credits), list(bills), numbered, list(perms))
