"""业务规则测试：交单判定、额度、授权、不符点、退单与更正状态机。"""

from datetime import date
from pathlib import Path
import sys
import unittest

# 兼容 python -I（隔离模式）：基于本文件位置把项目根加入 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lcwork.engine import (  # noqa: E402
    HOLDER_PERMISSION_MISSING,
    LIMIT_EXHAUSTED,
    MODEL_MISMATCH,
    ORIGINAL_HELD_BY_BANK,
    PARTIAL_NOT_ALLOWED,
    PRESENT_DATE_BOUNDARY,
    REV_NOT_EFFECTIVE,
    SHIP_DATE_BOUNDARY,
    Ledger,
)

try:  # 作为 tests 包成员加载
    from ._helpers import bill, build_repo, credit, msg, perm
except ImportError:  # python -m unittest discover -s tests 时按顶层模块加载
    from _helpers import bill, build_repo, credit, msg, perm


def evaluate(
    repo,
    group="G-1",
    cid="LC-1",
    revision=1,
    presenter="EX-1",
    on="2026-12-05",
    minor=10_000_000,
):
    return Ledger(repo).evaluate(
        group, cid, revision, presenter, date.fromisoformat(on), minor
    )


class EvaluateBasicsTest(unittest.TestCase):
    def test_clean_submission_passes_all_checks(self):
        repo = build_repo(
            credits=[credit()],
            bills=[bill()],
            perms=[perm()],
        )
        decision = evaluate(repo)
        self.assertTrue(decision.submittable)
        self.assertEqual(decision.blockers, ())
        self.assertEqual(decision.available_minor, 100_000_000)
        names = [c.name for c in decision.checks]
        self.assertEqual(
            names,
            ["正本位置", "持有人授权", "修订生效", "分批许可", "装船日期边界",
             "交单日期边界", "车型一致", "剩余额度"],
        )

    def test_unknown_group_is_blocked(self):
        repo = build_repo(credits=[credit()])
        decision = evaluate(repo, group="NOPE")
        self.assertFalse(decision.submittable)
        self.assertEqual(decision.blockers[0].code, "ORIGINAL_GROUP_UNKNOWN")

    def test_ineffective_revision_lists_missing_authorizations(self):
        repo = build_repo(
            credits=[credit(bank_effective="", buyer_ok=False, seller_ok=True)],
            bills=[bill()],
            perms=[perm()],
        )
        decision = evaluate(repo)
        blocker = next(b for b in decision.blockers if b.code == REV_NOT_EFFECTIVE)
        self.assertIn("买方未同意修订", blocker.reason)
        self.assertIn("缺开证行生效通知", blocker.reason)

    def test_boundary_days_are_inclusive(self):
        repo = build_repo(
            credits=[credit(ship="2026-12-01", present="2026-12-05")],
            bills=[bill(shipped="2026-12-01")],
            perms=[perm()],
        )
        decision = evaluate(repo, on="2026-12-05")
        self.assertTrue(decision.submittable)

    def test_ship_and_present_boundaries_are_checked_separately(self):
        repo = build_repo(
            credits=[credit(ship="2026-11-30", present="2026-12-05")],
            bills=[bill(shipped="2026-12-01")],
            perms=[perm()],
        )
        decision = evaluate(repo, on="2026-12-05")
        codes = {b.code for b in decision.blockers}
        self.assertEqual(codes, {SHIP_DATE_BOUNDARY})

        repo = build_repo(
            credits=[credit(ship="2026-12-01", present="2026-12-04")],
            bills=[bill(shipped="2026-12-01")],
            perms=[perm()],
        )
        decision = evaluate(repo, on="2026-12-05")
        codes = {b.code for b in decision.blockers}
        self.assertEqual(codes, {PRESENT_DATE_BOUNDARY})

    def test_model_mismatch_uses_target_revision_only(self):
        repo = build_repo(
            credits=[credit(models=("M1",))],
            bills=[bill(vehicles=(("V1", "M2"),))],
            perms=[perm()],
        )
        decision = evaluate(repo)
        self.assertIn(MODEL_MISMATCH, {b.code for b in decision.blockers})

    def test_partial_false_blocks_second_presentation(self):
        messages = [
            msg("M1", "PR-1", "received", minor=10_000_000),
        ]
        repo = build_repo(
            credits=[credit(partial=False)],
            bills=[bill(), bill(bid="BL-2", group="G-2")],
            messages=messages,
            perms=[perm(), perm(approval="A-2", group="G-2")],
        )
        decision = evaluate(repo, group="G-2")
        self.assertIn(PARTIAL_NOT_ALLOWED, {b.code for b in decision.blockers})

    def test_partial_true_allows_parallel_presentations_with_headroom(self):
        messages = [msg("M1", "PR-1", "received", minor=40_000_000)]
        repo = build_repo(
            credits=[credit(limit=100_000_000)],
            bills=[bill(), bill(bid="BL-2", group="G-2")],
            messages=messages,
            perms=[perm(), perm(approval="A-2", group="G-2")],
        )
        decision = evaluate(repo, group="G-2", minor=50_000_000)
        self.assertTrue(decision.submittable)
        self.assertEqual(decision.available_minor, 60_000_000)

    def test_limit_blocks_when_face_exceeds_available(self):
        messages = [msg("M1", "PR-1", "received", minor=80_000_000)]
        repo = build_repo(
            credits=[credit(limit=100_000_000)],
            bills=[bill(), bill(bid="BL-2", group="G-2")],
            messages=messages,
            perms=[perm(), perm(approval="A-2", group="G-2")],
        )
        decision = evaluate(repo, group="G-2", minor=30_000_000)
        self.assertIn(LIMIT_EXHAUSTED, {b.code for b in decision.blockers})


class PermissionAndOriginalsTest(unittest.TestCase):
    def test_missing_holder_permission_is_named_explicitly(self):
        repo = build_repo(credits=[credit()], bills=[bill()], perms=[])
        decision = evaluate(repo)
        blocker = next(b for b in decision.blockers if b.code == HOLDER_PERMISSION_MISSING)
        self.assertIn("没有任何持有人授权资料", blocker.reason)

    def test_permission_is_single_use(self):
        messages = [msg("M1", "PR-1", "received", minor=10_000_000)]
        repo = build_repo(
            credits=[credit()],
            bills=[
                bill(),
                bill(bid="BL-2", group="G-2", holder="FWD-1"),
            ],
            messages=messages,
            # 两个组都只给了 G-1 的授权，G-2 没有自己的授权。
            perms=[perm()],
        )
        decision = evaluate(repo, group="G-2")
        blocker = next(b for b in decision.blockers if b.code == HOLDER_PERMISSION_MISSING)
        self.assertIn("没有任何持有人授权资料", blocker.reason)

    def test_holder_must_match_current_deliverable_revision(self):
        repo = build_repo(
            credits=[credit()],
            bills=[bill(holder="FWD-9")],
            perms=[perm(holder="FWD-1")],
        )
        decision = evaluate(repo)
        blocker = next(b for b in decision.blockers if b.code == HOLDER_PERMISSION_MISSING)
        self.assertIn("FWD-9", blocker.reason)

    def test_originals_held_by_bank_block_reuse_until_return_evidence(self):
        messages = [msg("M1", "PR-1", "received", minor=10_000_000)]
        repo = build_repo(
            credits=[credit()],
            bills=[bill()],
            messages=messages,
            perms=[perm(), perm(approval="A-2", at="2026-12-03T00:00:00Z")],
        )
        decision = evaluate(repo)
        blocker = next(b for b in decision.blockers if b.code == ORIGINAL_HELD_BY_BANK)
        self.assertEqual(blocker.evidence, ("PR-1",))

        # 退回证据到位后，正本不再阻挡，但单次授权规则仍要求新授权。
        repo2 = build_repo(
            credits=[credit()],
            bills=[bill()],
            messages=[
                msg("M1", "PR-1", "received", minor=10_000_000),
                msg("M2", "PR-1", "originals_returned"),
            ],
            perms=[perm(), perm(approval="A-2", at="2026-12-03T00:00:00Z")],
        )
        decision2 = Ledger(repo2).evaluate(
            "G-1", "LC-1", 1, "EX-1", date(2026, 12, 6), 10_000_000
        )
        codes = {b.code for b in decision2.blockers}
        self.assertNotIn(ORIGINAL_HELD_BY_BANK, codes)
        self.assertTrue(
            all("A-1" in b.reason or "已用于交单" in b.reason
                for b in decision2.blockers if b.code == HOLDER_PERMISSION_MISSING)
        )

    def test_copies_and_reprints_do_not_create_new_originals(self):
        # 同组新版（补印/更正）不会出现第二套可交正本：老交单未退时整组被锁。
        messages = [msg("M1", "PR-1", "received", minor=10_000_000)]
        repo = build_repo(
            credits=[credit()],
            bills=[
                bill(revision=1, holder="FWD-1"),
                bill(bid="BL-1", revision=2, holder="BANK-1",
                     vehicles=(("V1", "M1"), ("V2", "M1"))),
            ],
            messages=messages,
            perms=[perm(), perm(approval="A-2", at="2026-12-03T00:00:00Z")],
        )
        decision = evaluate(repo)
        self.assertIn(ORIGINAL_HELD_BY_BANK, {b.code for b in decision.blockers})


class AmountLifecycleTest(unittest.TestCase):
    def _partial_repo(self, include_release=True, include_return=True):
        chain = [
            msg("M1", "PR-1", "received", minor=28_000_000),
            msg("M2", "PR-1", "discrepancy", discrepancies=("d1", "d2")),
            msg("M3", "PR-1", "waiver", discrepancies=("d1",)),
            msg("M4", "PR-1", "paid", minor=17_000_000, fee=22_500),
        ]
        if include_release:
            chain.append(msg("M5", "PR-1", "amount_released", minor=11_000_000))
        if include_return:
            chain.append(msg("M6", "PR-1", "originals_returned"))
        return build_repo(
            credits=[credit(limit=50_000_000)],
            bills=[bill()],
            messages=chain,
            perms=[perm()],
        )

    def test_partial_negotiation_remainder_released_is_reusable(self):
        ledger = Ledger(self._partial_repo())
        view = ledger.presentation_view("PR-1")
        self.assertEqual(view.face_minor, 28_000_000)
        self.assertEqual(view.paid_minor, 17_000_000)
        self.assertEqual(view.released_minor, 11_000_000)
        self.assertEqual(view.held_minor, 0)
        self.assertEqual(view.fee_minor, 22_500)
        self.assertEqual(view.net_receipt_minor, 17_000_000 - 22_500)
        self.assertEqual(view.amount_fate.remainder_minor, 0)
        self.assertEqual(ledger.occupied_by_credit("LC-1"), 0)
        self.assertEqual(ledger.available_balance("LC-1"), 50_000_000)

    def test_remainder_without_release_must_wait_for_return(self):
        ledger = Ledger(self._partial_repo(include_release=False, include_return=False))
        view = ledger.presentation_view("PR-1")
        self.assertEqual(view.held_minor, 11_000_000)
        self.assertIn("需等银行退单或释放通知", view.amount_fate.conclusion)
        self.assertEqual(ledger.available_balance("LC-1"), 50_000_000 - 11_000_000)

    def test_missing_reply_blocks_only_its_own_share(self):
        # PR-1 的剩余没有任何回话，但同证另一正本组只要还有额度就仍可交。
        ledger = Ledger(self._partial_repo(include_release=False, include_return=False))
        repo2 = build_repo(
            credits=[credit(limit=50_000_000)],
            bills=[
                bill(),
                bill(bid="BL-2", group="G-2"),
            ],
            messages=[
                msg("M1", "PR-1", "received", minor=28_000_000),
                msg("M4", "PR-1", "paid", minor=17_000_000, fee=22_500),
            ],
            perms=[perm(), perm(approval="A-2", group="G-2")],
        )
        decision = Ledger(repo2).evaluate(
            "G-2", "LC-1", 1, "EX-1", date(2026, 12, 6), 20_000_000
        )
        self.assertTrue(decision.submittable)
        self.assertEqual(decision.available_minor, 50_000_000 - 11_000_000)

    def test_unwaived_discrepancies_keep_settlement_blocked(self):
        ledger = Ledger(self._partial_repo())
        view = ledger.presentation_view("PR-1")
        self.assertEqual(view.listed_discrepancies, ("d1", "d2"))
        self.assertEqual(view.waived_discrepancies, ("d1",))
        self.assertEqual(view.open_discrepancies, ("d2",))
        self.assertTrue(view.settlement_blocked)
        self.assertEqual(view.status, "paid_with_open_discrepancies")

    def test_fees_are_separate_from_receivables(self):
        line = Ledger(self._partial_repo()).cashier_view("LC-1")
        self.assertEqual(line.claimable_receipt_minor, 17_000_000)
        self.assertEqual(line.fee_minor, 22_500)
        self.assertEqual(line.net_receipt_minor, 17_000_000 - 22_500)
        self.assertEqual(line.pending_minor, 0)


class CorrectionTest(unittest.TestCase):
    def test_correction_before_any_payment_suspends_and_waits_for_return(self):
        repo = build_repo(
            credits=[credit()],
            bills=[
                bill(revision=1, holder="FWD-1"),
                bill(bid="BL-1", revision=2, holder="BANK-1",
                     vehicles=(("V1", "M1"), ("V2", "M1"))),
            ],
            messages=[msg("M1", "PR-1", "received", minor=10_000_000)],
            perms=[perm()],
        )
        view = Ledger(repo).presentation_view("PR-1")
        self.assertTrue(view.correction_after_presentation)
        self.assertEqual(view.correction_status, "suspended_pending_return")
        self.assertEqual(view.presented_bill_ref, "BL-1@rev1")
        self.assertEqual(view.latest_bill_ref, "BL-1@rev2")
        self.assertIn("挂起", view.next_action)

    def test_correction_after_negotiation_keeps_receipt_and_lists_followup(self):
        repo = build_repo(
            credits=[credit()],
            bills=[
                bill(revision=1, holder="FWD-1"),
                bill(bid="BL-1", revision=2, holder="BANK-1",
                     vehicles=(("V1", "M1"), ("V2", "M1"))),
            ],
            messages=[
                msg("M1", "PR-1", "received", minor=10_000_000),
                msg("M2", "PR-1", "paid", minor=10_000_000, fee=1_000),
            ],
            perms=[perm()],
        )
        view = Ledger(repo).presentation_view("PR-1")
        self.assertEqual(view.correction_status, "negotiated_pending_correction")
        self.assertEqual(view.paid_minor, 10_000_000)
        self.assertEqual(view.net_receipt_minor, 10_000_000 - 1_000)
        self.assertIn("更正待处理", view.status_detail)


class RevisionRulesTest(unittest.TestCase):
    def test_submitted_presentation_stays_on_bank_confirmed_revision(self):
        repo = build_repo(
            credits=[
                credit(revision=1, models=("M1",)),
                credit(revision=2, models=("M1", "M2"), bank_effective=""),
                credit(revision=3, models=("M1", "M2"), bank_effective="B-3"),
            ],
            bills=[bill()],
            messages=[msg("M1", "PR-1", "received", minor=10_000_000, revision=1)],
            perms=[perm()],
        )
        view = Ledger(repo).presentation_view("PR-1")
        self.assertEqual(view.credit_ref, "LC-1@rev1")
        # 新交单默认仍可按 rev3 判定，老交单不会被新版重判。
        decision = Ledger(repo).evaluate(
            "G-1", "LC-1", 3, "EX-1", date(2026, 12, 6), 5_000_000
        )
        # 但正本还在银行，所以仍被挡住——这正是版本与正本两条独立规则。
        self.assertIn(ORIGINAL_HELD_BY_BANK, {b.code for b in decision.blockers})

    def test_available_balance_uses_latest_effective_revision_limit(self):
        repo = build_repo(
            credits=[
                credit(revision=1, limit=50_000_000),
                credit(revision=2, limit=52_000_000, bank_effective="B-2"),
            ],
            bills=[bill(), bill(bid="BL-2", group="G-2")],
            messages=[msg("M1", "PR-1", "received", minor=28_000_000)],
            perms=[perm(), perm(approval="A-2", group="G-2")],
        )
        ledger = Ledger(repo)
        self.assertEqual(ledger.available_balance("LC-1"), 52_000_000 - 28_000_000)
        line = ledger.cashier_view("LC-1")
        self.assertEqual(line.effective_revision, 2)
        self.assertEqual(line.pending_minor, 28_000_000)

    def test_no_effective_revision_means_no_available_balance(self):
        repo = build_repo(
            credits=[credit(bank_effective="")],
            bills=[bill()],
            perms=[perm()],
        )
        self.assertIsNone(Ledger(repo).available_balance("LC-1"))


if __name__ == "__main__":
    unittest.main()
