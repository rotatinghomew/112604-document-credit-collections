"""信用证交单业务规则与查询服务。

设计原则：

* 所有结论都带证据（消息编号、授权编号、提单版本号），任何复核人都能
  顺着同一次交单看到“为什么还能交 / 缺哪份授权 / 下一步谁办”。
* 已递交的交单以银行消息确认的信用证版本为准，不用新版条款重判当时合规性。
* 金额占用、正本占用、授权占用分别计算；任何一份回话缺失只挡住它对应的
  那部分，不影响证下其他仍有余量的交单。
"""

from dataclasses import dataclass
from datetime import date

from .model import BankMessage, BillRev, Permission, Repository

# -- 阻断原因码 -------------------------------------------------------------
REV_UNKNOWN = "CREDIT_REV_UNKNOWN"
REV_NOT_EFFECTIVE = "REV_NOT_EFFECTIVE"
PARTIAL_NOT_ALLOWED = "PARTIAL_NOT_ALLOWED"
LIMIT_EXHAUSTED = "LIMIT_EXHAUSTED"
SHIP_DATE_BOUNDARY = "SHIP_DATE_BOUNDARY"
PRESENT_DATE_BOUNDARY = "PRESENT_DATE_BOUNDARY"
MODEL_MISMATCH = "MODEL_MISMATCH"
HOLDER_PERMISSION_MISSING = "HOLDER_PERMISSION_MISSING"
ORIGINAL_HELD_BY_BANK = "ORIGINAL_HELD_BY_BANK"
GROUP_UNKNOWN = "ORIGINAL_GROUP_UNKNOWN"


@dataclass(frozen=True)
class CheckItem:
    """单项核对结果。ok 为 False 且 blocking 为 True 时构成阻断。"""

    name: str
    ok: bool
    detail: str
    evidence: tuple[str, ...] = ()
    blocking: bool = False
    code: str = ""


@dataclass(frozen=True)
class Blocker:
    code: str
    reason: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class Decision:
    """一次拟递交交单的完整判定，checks 保留全部核对明细。"""

    submittable: bool
    blockers: tuple[Blocker, ...]
    checks: tuple[CheckItem, ...]
    available_minor: int | None
    would_occupy_minor: int | None
    bill_rev: str | None
    credit_rev: str | None


@dataclass(frozen=True)
class AmountFate:
    """部分议付后剩余金额的具体结论与依据。"""

    remainder_minor: int
    conclusion: str  # 可重新交 / 等退单 / 无剩余
    basis: tuple[str, ...]


@dataclass
class PresentationView:
    """沿同一次交单展开的全过程视图。"""

    presentation: str
    credit: str
    credit_revision: int
    credit_ref: str
    original_group: str
    face_minor: int
    paid_minor: int
    released_minor: int
    fee_minor: int
    held_minor: int
    net_receipt_minor: int
    amount_fate: AmountFate
    originals_at_bank: bool
    originals_returned_messages: tuple[str, ...]
    presented_bill_ref: str | None
    latest_bill_ref: str | None
    correction_after_presentation: bool
    correction_status: str  # none / suspended_pending_return / negotiated_pending_correction
    approval_ref: str | None
    approval_issue: str  # none / missing / reused
    listed_discrepancies: tuple[str, ...]
    waived_discrepancies: tuple[str, ...]
    open_discrepancies: tuple[str, ...]
    settlement_blocked: bool
    status: str
    status_detail: str
    next_action: str
    next_owner: str
    message_chain: tuple[str, ...]
    paid_messages: tuple[str, ...]
    waiver_messages: tuple[str, ...]


@dataclass(frozen=True)
class CashierLine:
    credit: str
    currency: str
    effective_revision: int | None
    limit_minor: int | None
    occupied_minor: int
    available_minor: int | None
    claimable_receipt_minor: int
    fee_minor: int
    net_receipt_minor: int
    pending_minor: int


class Ledger:
    """基于只读仓储计算交单状态、额度与视图。"""

    def __init__(self, repo: Repository) -> None:
        self.repo = repo
        self._approval_owner: dict[str, str] = {}
        self._presented_bill: dict[str, BillRev] = {}
        self._approval_issue: dict[str, str] = {}
        self._build_presentation_index()

    # ===================================================================
    # 交单索引：授权占用、递交时所用提单版本
    # ===================================================================
    def _build_presentation_index(self) -> None:
        used_approvals: set[str] = set()
        for pr in self.repo.presentation_ids():
            chain = self.repo.messages_of(pr)
            received = next(m for m in chain if m.event == "received")
            group = received.original_group

            perm: Permission | None = None
            for candidate in self.repo.permissions_of_group(group):
                if candidate.approval in used_approvals:
                    continue
                if self._bill_holder_exists(group, candidate.holder):
                    perm = candidate
                    break
            if perm is None:
                # 资料里找不到未使用且持有人匹配的授权。
                self._approval_issue[pr] = "missing"
                fallback = next(
                    (p for p in self.repo.permissions_of_group(group)), None
                )
                if fallback is not None:
                    self._presented_bill[pr] = self._pick_bill_rev(group, fallback.holder)
            else:
                used_approvals.add(perm.approval)
                self._approval_owner[perm.approval] = pr
                self._presented_bill[pr] = self._pick_bill_rev(group, perm.holder)
                self._approval_issue[pr] = "none"

    def _bill_holder_exists(self, group: str, holder: str) -> bool:
        return any(b.holder == holder for b in self.repo.group_revisions(group))

    def _pick_bill_rev(self, group: str, holder: str) -> BillRev:
        """授权持有人对应的最高版提单，即交单当时实际递交的版本。"""
        matching = [b for b in self.repo.group_revisions(group) if b.holder == holder]
        if not matching:  # 容错：退回带问题标记的视图
            return self.repo.latest_group_rev(group)  # type: ignore[return-value]
        return matching[-1]

    def presented_bill(self, presentation: str) -> BillRev | None:
        return self._presented_bill.get(presentation)

    def approval_for(self, presentation: str) -> Permission | None:
        for approval, pr in self._approval_owner.items():
            if pr == presentation:
                return next(
                    p
                    for p in self.repo.permissions_of_group(
                        self.repo.messages_of(presentation)[0].original_group
                    )
                    if p.approval == approval
                )
        return None

    # ===================================================================
    # 额度：票面金额在银行确认前一直占用
    # ===================================================================
    def held_amount(self, presentation: str) -> int:
        """仍被该交单占用的金额：票面 - 已议付 - 已确认释放。"""
        chain = self.repo.messages_of(presentation)
        face = next(m for m in chain if m.event == "received").minor
        confirmed = sum(
            m.minor for m in chain if m.event in {"paid", "amount_released"}
        )
        return face - confirmed

    def occupied_by_credit(self, credit: str) -> int:
        total = 0
        for pr in self.repo.presentation_ids():
            chain = self.repo.messages_of(pr)
            if chain[0].credit == credit:
                total += self.held_amount(pr)
        return total

    def available_balance(self, credit: str) -> int | None:
        rev = self.repo.latest_effective_rev(credit)
        if rev is None:
            return None
        return rev.limit_minor - self.occupied_by_credit(credit)

    # ===================================================================
    # 拟递交交单判定（复核员放行前的完整核对）
    # ===================================================================
    def evaluate(
        self,
        original_group: str,
        credit: str,
        revision: int,
        presenter: str,
        present_date: date,
        face_minor: int,
    ) -> Decision:
        checks: list[CheckItem] = []
        blockers: list[Blocker] = []
        latest_bill = self.repo.latest_group_rev(original_group)
        credit_rev = self.repo.credit_rev(credit, revision)
        available = self.available_balance(credit)

        # 1. 正本组存在且当前可交（没有正本还压在银行）。
        if latest_bill is None:
            checks.append(
                CheckItem(
                    "正本组资料",
                    False,
                    f"正本组 {original_group} 在提单资料中不存在",
                    (original_group,),
                    True,
                    GROUP_UNKNOWN,
                )
            )
            return Decision(
                False,
                (Blocker(GROUP_UNKNOWN, f"正本组 {original_group} 不存在", (original_group,)),),
                tuple(checks),
                available,
                None,
                None,
                None,
            )

        held_prs = self._presentations_holding_group(original_group)
        held_check_ok = not held_prs
        checks.append(
            CheckItem(
                "正本位置",
                held_check_ok,
                (
                    "正本不在银行，可安排递交"
                    if held_check_ok
                    else f"正本仍在银行，占用交单：{', '.join(held_prs)}；退单证据到位前不能再投入"
                ),
                tuple(held_prs),
                blocking=not held_check_ok,
                code=ORIGINAL_HELD_BY_BANK,
            )
        )
        if held_prs:
            blockers.append(
                Blocker(
                    ORIGINAL_HELD_BY_BANK,
                    f"正本组 {original_group} 的正本在银行未退回",
                    tuple(held_prs),
                )
            )

        # 2. 持有人授权：存在、持有人与当前可交单版本一致、未被其他交单使用。
        perm, perm_issue, perm_evidence = self._usable_permission(
            original_group, latest_bill.holder, presenter
        )
        if perm_issue is None:
            perm_detail = f"授权 {perm.approval} 有效：持有人 {perm.holder} 授权交单人 {presenter}"
        else:
            perm_detail = perm_issue
        checks.append(
            CheckItem(
                "持有人授权",
                perm_issue is None,
                perm_detail,
                perm_evidence,
                blocking=perm_issue is not None,
                code=HOLDER_PERMISSION_MISSING,
            )
        )
        if perm_issue is not None:
            blockers.append(
                Blocker(HOLDER_PERMISSION_MISSING, perm_issue, perm_evidence)
            )

        # 3. 信用证版本存在且已生效。
        if credit_rev is None:
            checks.append(
                CheckItem(
                    "信用证版本",
                    False,
                    f"{credit}@rev{revision} 不存在",
                    (),
                    True,
                    REV_UNKNOWN,
                )
            )
            blockers.append(
                Blocker(REV_UNKNOWN, f"信用证版本 {credit}@rev{revision} 不存在", ())
            )
        else:
            missing = credit_rev.missing_effectiveness
            checks.append(
                CheckItem(
                    "修订生效",
                    not missing,
                    (
                        f"{credit_rev.ref} 已凭开证行通知 {credit_rev.bank_effective} 生效"
                        if not missing
                        else f"{credit_rev.ref} 未生效：{'；'.join(missing)}"
                    ),
                    (credit_rev.bank_effective,) if credit_rev.bank_effective else (),
                    blocking=bool(missing),
                    code=REV_NOT_EFFECTIVE,
                )
            )
            if missing:
                blockers.append(
                    Blocker(
                        REV_NOT_EFFECTIVE,
                        f"修订缺生效要件：{'；'.join(missing)}",
                        (credit_rev.ref,),
                    )
                )

            # 4. 分批许可（证下已有其他交单时才有约束力）。
            sibling_prs = [
                pr
                for pr in self.repo.presentation_ids()
                if self.repo.messages_of(pr)[0].credit == credit
            ]
            if not credit_rev.partial and sibling_prs:
                detail = f"该证不允许分批，证下已有交单：{', '.join(sibling_prs)}"
                checks.append(
                    CheckItem(
                        "分批许可", False, detail, tuple(sibling_prs), True, PARTIAL_NOT_ALLOWED
                    )
                )
                blockers.append(Blocker(PARTIAL_NOT_ALLOWED, detail, tuple(sibling_prs)))
            else:
                checks.append(
                    CheckItem(
                        "分批许可",
                        True,
                        "允许分批" if credit_rev.partial else "允许整批交单（证下暂无其他交单）",
                    )
                )

            # 5. 公开日期边界，边界当天可用。
            ship_ok = latest_bill.shipped <= credit_rev.latest_ship
            checks.append(
                CheckItem(
                    "装船日期边界",
                    ship_ok,
                    f"装船日 {latest_bill.shipped}，证定最迟 {credit_rev.latest_ship}（边界当天可用）",
                    (latest_bill.ref,),
                    blocking=not ship_ok,
                    code=SHIP_DATE_BOUNDARY,
                )
            )
            if not ship_ok:
                blockers.append(
                    Blocker(
                        SHIP_DATE_BOUNDARY,
                        f"装船日 {latest_bill.shipped} 晚于最迟装船日 {credit_rev.latest_ship}",
                        (latest_bill.ref, credit_rev.ref),
                    )
                )
            present_ok = present_date <= credit_rev.latest_present
            checks.append(
                CheckItem(
                    "交单日期边界",
                    present_ok,
                    f"拟交单日 {present_date}，证定最迟 {credit_rev.latest_present}（边界当天可用）",
                    (credit_rev.ref,),
                    blocking=not present_ok,
                    code=PRESENT_DATE_BOUNDARY,
                )
            )
            if not present_ok:
                blockers.append(
                    Blocker(
                        PRESENT_DATE_BOUNDARY,
                        f"拟交单日 {present_date} 晚于最迟交单日 {credit_rev.latest_present}",
                        (credit_rev.ref,),
                    )
                )

            # 6. 车型必须落在该版证列明的车型内。
            bad_models = sorted(set(latest_bill.models) - set(credit_rev.models))
            model_ok = not bad_models
            checks.append(
                CheckItem(
                    "车型一致",
                    model_ok,
                    (
                        f"车型 {list(latest_bill.models)} 均在证内 {list(credit_rev.models)}"
                        if model_ok
                        else f"车型 {bad_models} 不在 {credit_rev.ref} 列明范围 {list(credit_rev.models)}"
                    ),
                    (latest_bill.ref, credit_rev.ref),
                    blocking=not model_ok,
                    code=MODEL_MISMATCH,
                )
            )
            if bad_models:
                blockers.append(
                    Blocker(
                        MODEL_MISMATCH,
                        f"车型 {bad_models} 与信用证车型条件不一致",
                        (latest_bill.ref, credit_rev.ref),
                    )
                )

            # 7. 剩余额度（版本未生效时只展示信息，阻断统一由“修订生效”项给出）。
            if available is None:
                limit_detail = "该证尚无生效版本，无法计算可用余额"
                limit_ok = False
                limit_blocking = credit_rev.effective
            else:
                limit_ok = available >= face_minor
                limit_blocking = True
                limit_detail = (
                    f"票面 {face_minor}，当前可用 {available}"
                    f"（额度 {credit_rev.limit_minor} - 已占用 {self.occupied_by_credit(credit)}）"
                )
            checks.append(
                CheckItem(
                    "剩余额度",
                    limit_ok,
                    limit_detail,
                    (credit_rev.ref,),
                    blocking=not limit_ok and limit_blocking,
                    code=LIMIT_EXHAUSTED,
                )
            )
            if not limit_ok and limit_blocking:
                blockers.append(
                    Blocker(
                        LIMIT_EXHAUSTED,
                        f"剩余额度不足：{limit_detail}",
                        (credit_rev.ref,),
                    )
                )

        return Decision(
            submittable=not blockers,
            blockers=tuple(blockers),
            checks=tuple(checks),
            available_minor=available,
            would_occupy_minor=face_minor,
            bill_rev=latest_bill.ref,
            credit_rev=credit_rev.ref if credit_rev else None,
        )

    def _usable_permission(
        self, group: str, holder: str, presenter: str
    ) -> tuple[Permission | None, str | None, tuple[str, ...]]:
        """返回（授权，问题说明，证据）。授权按单次使用规则核销。"""
        perms = self.repo.permissions_of_group(group)
        if not perms:
            return None, f"正本组 {group} 没有任何持有人授权资料", ()
        for perm in perms:
            if perm.holder != holder:
                continue
            if perm.presenter != presenter:
                continue
            owner = self._approval_owner.get(perm.approval)
            if owner is not None:
                return (
                    None,
                    f"授权 {perm.approval} 已用于交单 {owner}，单次授权不能重复使用",
                    (perm.approval, owner),
                )
            return perm, None, (perm.approval,)
        # 区分“持有人不对”和“交单人不对/已用完”，方便补授权。
        holders = sorted({p.holder for p in perms})
        return (
            None,
            f"找不到持有人 {holder} 对交单人 {presenter} 的未用授权；现有授权持有人：{holders}",
            tuple(p.approval for p in perms),
        )

    def _presentations_holding_group(self, group: str) -> list[str]:
        holding = []
        for pr in self.repo.presentation_ids():
            chain = self.repo.messages_of(pr)
            if chain[0].original_group != group:
                continue
            if not any(m.event == "originals_returned" for m in chain):
                holding.append(pr)
        return holding

    # ===================================================================
    # 已递交交单的全过程视图
    # ===================================================================
    def presentation_view(self, presentation: str) -> PresentationView:
        chain = self.repo.messages_of(presentation)
        if not chain:
            raise KeyError(f"交单 {presentation} 不存在")
        received = next(m for m in chain if m.event == "received")
        credit_rev = self.repo.credit_rev(received.credit, received.revision)
        face = received.minor
        paid = sum(m.minor for m in chain if m.event == "paid")
        released = sum(m.minor for m in chain if m.event == "amount_released")
        fees = sum(m.fee_minor for m in chain)
        held = face - paid - released
        settled = paid + released

        listed = tuple(d for m in chain if m.event == "discrepancy" for d in m.discrepancies)
        waived = tuple(d for m in chain if m.event == "waiver" for d in m.discrepancies)
        open_disc = tuple(d for d in listed if d not in set(waived))

        paid_msgs = tuple(m.message for m in chain if m.event == "paid")
        waiver_msgs = tuple(m.message for m in chain if m.event == "waiver")
        returned_msgs = tuple(m.message for m in chain if m.event == "originals_returned")
        originals_at_bank = not returned_msgs

        presented = self._presented_bill.get(presentation)
        latest = self.repo.latest_group_rev(received.original_group)
        correction = bool(
            presented is not None and latest is not None and latest.revision > presented.revision
        )

        has_paid = bool(paid_msgs)
        if correction and not has_paid:
            correction_status = "suspended_pending_return"
        elif correction:
            correction_status = "negotiated_pending_correction"
        else:
            correction_status = "none"

        fate = self._amount_fate(presentation, face, paid, released)
        status, detail = self._status(
            chain, listed, open_disc, paid, released, face, correction_status
        )
        next_action, owner = self._next_action(
            presentation, chain, open_disc, fate, correction_status, originals_at_bank
        )
        approval = self.approval_for(presentation)
        issue = self._approval_issue.get(presentation, "missing")

        return PresentationView(
            presentation=presentation,
            credit=received.credit,
            credit_revision=received.revision,
            credit_ref=credit_rev.ref if credit_rev else f"{received.credit}@rev{received.revision}",
            original_group=received.original_group,
            face_minor=face,
            paid_minor=paid,
            released_minor=released,
            fee_minor=fees,
            held_minor=held,
            net_receipt_minor=paid - fees,
            amount_fate=fate,
            originals_at_bank=originals_at_bank,
            originals_returned_messages=returned_msgs,
            presented_bill_ref=presented.ref if presented else None,
            latest_bill_ref=latest.ref if latest else None,
            correction_after_presentation=correction,
            correction_status=correction_status,
            approval_ref=approval.approval if approval else None,
            approval_issue=issue,
            listed_discrepancies=listed,
            waived_discrepancies=waived,
            open_discrepancies=open_disc,
            settlement_blocked=bool(open_disc),
            status=status,
            status_detail=detail,
            next_action=next_action,
            next_owner=owner,
            message_chain=tuple(m.message for m in chain),
            paid_messages=paid_msgs,
            waiver_messages=waiver_msgs,
        )

    def all_presentation_views(self) -> list[PresentationView]:
        return [self.presentation_view(pr) for pr in self.repo.presentation_ids()]

    def _amount_fate(
        self, presentation: str, face: int, paid: int, released: int
    ) -> AmountFate:
        chain = self.repo.messages_of(presentation)
        remainder = face - paid - released
        release_msgs = tuple(m.message for m in chain if m.event == "amount_released")
        if remainder == 0:
            basis = tuple(
                m.message for m in chain if m.event in {"paid", "amount_released"}
            )
            return AmountFate(0, "票面金额已有银行结论，无悬置余额", basis)
        if release_msgs:
            return AmountFate(
                remainder,
                f"剩余 {remainder} 已由银行确认释放，可凭释放消息重新安排交单",
                release_msgs,
            )
        return AmountFate(
            remainder,
            f"剩余 {remainder} 未获释放结论，需等银行退单或释放通知后才能再投入",
            tuple(m.message for m in chain if m.event == "paid"),
        )

    def _status(
        self,
        chain: list[BankMessage],
        listed: tuple[str, ...],
        open_disc: tuple[str, ...],
        paid: int,
        released: int,
        face: int,
        correction_status: str,
    ) -> tuple[str, str]:
        events = {m.event for m in chain}
        if correction_status == "suspended_pending_return":
            return (
                "correction_suspended",
                "交单后船代签发更正版，未决审单挂起，等银行退单证据；原交正本不得直接换成新版",
            )
        if correction_status == "negotiated_pending_correction":
            return (
                "negotiated_correction_pending",
                "已议付但船代更正版尚未处理，列入更正待处理；已收款记录保留",
            )
        if open_disc and paid:
            return (
                "paid_with_open_discrepancies",
                f"银行已议付但不符点 {list(open_disc)} 未被接受，合规上仍不允许结算，需补 waiver 或退单处理",
            )
        if open_disc:
            return (
                "discrepancy_open",
                f"未被接受的不符点 {list(open_disc)} 仍挂起，不能结算",
            )
        if listed and not open_disc:
            return ("discrepancy_waived", f"列明不符点 {list(listed)} 已全部被接受")
        if paid and paid + released == face:
            if "originals_returned" in events:
                return ("closed_returned", "票面金额结清且正本已退回，流程完结")
            return ("negotiated_full", "票面金额已全额议付，等正本退回或后续银行消息")
        if paid:
            return ("negotiated_partial", "银行仅按部分金额议付，剩余金额结论见 amount_fate")
        if released:
            return ("amount_released_pending", "银行已释放部分金额，交单仍在处理中")
        if "originals_returned" in events:
            return ("returned_unpaid", "银行退单且未议付，金额与正本需取得可重用确认后再安排")
        return ("under_examination", "银行已收件，审单中")

    def _next_action(
        self,
        presentation: str,
        chain: list[BankMessage],
        open_disc: tuple[str, ...],
        fate: AmountFate,
        correction_status: str,
        originals_at_bank: bool,
    ) -> tuple[str, str]:
        if correction_status == "suspended_pending_return":
            return "挂起该交单，凭 originals_returned 消息登记退单证据后再按新版提单重走授权", "单证组"
        if correction_status == "negotiated_pending_correction":
            return "将更正版提单列入更正待处理清单，联系银行确认更正处理方式；收款记录保留不动", "单证组"
        if open_disc:
            return (
                f"就未接受不符点 {list(open_disc)} 取得买方 waiver（仅覆盖列明项）或退单证据，否则不得结算",
                "单证组/买方",
            )
        if fate.remainder_minor > 0 and fate.conclusion.startswith("剩余"):
            return "向银行确认剩余金额是释放可重交还是退单，拿到 amount_released 或 originals_returned 消息", "出纳/银行"
        if originals_at_bank and any(m.event == "paid" for m in chain):
            return "等待银行退回正本（originals_returned），收款可先认领", "出纳"
        if originals_at_bank:
            return "等待银行审单结论（discrepancy / waiver / paid）", "银行"
        if fate.remainder_minor == 0:
            return "流程完结；如需重新交单，须凭新版提单与新的持有人授权办理", "单证组"
        return "核对退回正本与金额的可重用确认，再决定是否重新递交", "单证组"

    # ===================================================================
    # 出纳视图与证级总览
    # ===================================================================
    def cashier_view(self, credit: str) -> CashierLine:
        rev = self.repo.latest_effective_rev(credit)
        prs = [
            pr
            for pr in self.repo.presentation_ids()
            if self.repo.messages_of(pr)[0].credit == credit
        ]
        claimable = sum(self.presentation_view(pr).paid_minor for pr in prs)
        fees = sum(self.presentation_view(pr).fee_minor for pr in prs)
        pending = sum(self.held_amount(pr) for pr in prs)
        occupied = self.occupied_by_credit(credit)
        return CashierLine(
            credit=credit,
            currency=rev.currency if rev else "",
            effective_revision=rev.revision if rev else None,
            limit_minor=rev.limit_minor if rev else None,
            occupied_minor=occupied,
            available_minor=self.available_balance(credit),
            claimable_receipt_minor=claimable,
            fee_minor=fees,
            net_receipt_minor=claimable - fees,
            pending_minor=pending,
        )

    def credit_overview(self, credit: str) -> dict:
        revs = self.repo.credit_revisions(credit)
        rev_rows = [
            {
                "revision": r.revision,
                "effective": r.effective,
                "bank_effective": r.bank_effective or None,
                "missing": r.missing_effectiveness,
                "limit_minor": r.limit_minor,
                "partial": r.partial,
                "latest_ship": r.latest_ship.isoformat(),
                "latest_present": r.latest_present.isoformat(),
                "models": list(r.models),
            }
            for r in revs
        ]
        active_groups = sorted(
            {
                m.original_group
                for m in self.repo.messages
                if m.credit == credit and m.event == "received"
            }
        )
        presentations = [
            pr
            for pr in self.repo.presentation_ids()
            if self.repo.messages_of(pr)[0].credit == credit
        ]
        return {
            "credit": credit,
            "revisions": rev_rows,
            "presentations": presentations,
            "occupied_groups": [
                group for group in active_groups if self._presentations_holding_group(group)
            ],
            "cashier_line": self.cashier_view(credit),
        }
