"""进件严格校验测试 + 对仓库自带合成资料的端到端断言。"""

import json
from datetime import date
from pathlib import Path
import sys
import tempfile
import unittest

# 兼容 python -I（隔离模式）：基于本文件位置把项目根加入 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lcwork.cli import main as cli_main  # noqa: E402
from lcwork.engine import Ledger  # noqa: E402
from lcwork.loader import FeedError, load_repository  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def _write(tmp: Path, name: str, rows: list[dict]) -> None:
    (tmp / f"{name}.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )


def _scenario(tmp: Path, *, messages: list[dict], credits=None, bills=None, perms=None) -> Path:
    (tmp / "formats.json").write_text((SAMPLES / "formats.json").read_text(encoding="utf-8"))
    _write(tmp, "credits", credits or [
        {"credit": "LC-1", "revision": 1, "currency": "USD", "limit_minor": 100000000,
         "partial": True, "latest_ship": "2026-12-10", "latest_present": "2026-12-20",
         "models": ["M1"], "buyer_ok": True, "seller_ok": True, "bank_effective": "B-1"},
    ])
    _write(tmp, "bills", bills or [
        {"bill": "BL-1", "revision": 1, "original_group": "G-1", "holder": "FWD-1",
         "voyage": "VO-1", "shipped": "2026-12-01",
         "vehicles": [{"vin": "V1", "model": "M1"}]},
    ])
    _write(tmp, "bank_messages", messages)
    _write(tmp, "bilateral_permissions", perms or [
        {"approval": "A-1", "group": "G-1", "holder": "FWD-1",
         "presenter": "EX-1", "at": "2026-12-02T00:00:00Z"},
    ])
    return tmp


def _received(message="M1", **over):
    row = {"message": message, "presentation": "PR-1", "credit": "LC-1", "revision": 1,
           "event": "received", "minor": 10000000, "fee_minor": 0,
           "original_group": "G-1", "discrepancies": []}
    row.update(over)
    return row


class IntakeValidationTest(unittest.TestCase):
    def test_first_message_must_be_received(self):
        with tempfile.TemporaryDirectory() as d:
            _scenario(Path(d), messages=[_received(event="paid", minor=10000000)])
            with self.assertRaisesRegex(FeedError, "received"):
                load_repository(d)

    def test_settled_amount_cannot_exceed_face(self):
        with tempfile.TemporaryDirectory() as d:
            _scenario(Path(d), messages=[
                _received(),
                _received(message="M2", event="paid", minor=10000001),
            ])
            with self.assertRaisesRegex(FeedError, "票面金额"):
                load_repository(d)

    def test_waiver_must_stay_within_listed_discrepancies(self):
        with tempfile.TemporaryDirectory() as d:
            _scenario(Path(d), messages=[
                _received(),
                _received(message="M2", event="discrepancy", minor=0, discrepancies=["d1"]),
                _received(message="M3", event="waiver", minor=0, discrepancies=["d9"]),
            ])
            with self.assertRaisesRegex(FeedError, "未列明"):
                load_repository(d)

    def test_unknown_credit_reference_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            _scenario(Path(d), messages=[_received(credit="LC-X")])
            with self.assertRaisesRegex(FeedError, "信用证版本不存在"):
                load_repository(d)

    def test_bill_revisions_must_share_original_group(self):
        with tempfile.TemporaryDirectory() as d:
            _scenario(
                Path(d),
                messages=[_received()],
                bills=[
                    {"bill": "BL-1", "revision": 1, "original_group": "G-1", "holder": "FWD-1",
                     "voyage": "VO-1", "shipped": "2026-12-01",
                     "vehicles": [{"vin": "V1", "model": "M1"}]},
                    {"bill": "BL-1", "revision": 2, "original_group": "G-9", "holder": "FWD-1",
                     "voyage": "VO-1", "shipped": "2026-12-01",
                     "vehicles": [{"vin": "V1", "model": "M1"}]},
                ],
            )
            with self.assertRaisesRegex(FeedError, "同一正本组"):
                load_repository(d)

    def test_bad_field_type_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            _scenario(Path(d), messages=[_received(minor="1000")])
            with self.assertRaisesRegex(FeedError, "类型应为"):
                load_repository(d)


class SampleFeedEndToEndTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ledger = Ledger(load_repository(SAMPLES))

    def test_sample_presentation_full_lifecycle(self):
        view = self.ledger.presentation_view("PR-1")
        self.assertEqual(view.credit_ref, "LC-19@rev1")
        self.assertEqual(view.original_group, "O-14")
        self.assertEqual(view.face_minor, 28_000_000)
        self.assertEqual(view.paid_minor, 17_000_000)
        self.assertEqual(view.released_minor, 11_000_000)
        self.assertEqual(view.held_minor, 0)
        self.assertEqual(view.fee_minor, 22_500)
        self.assertEqual(view.net_receipt_minor, 17_000_000 - 22_500)
        self.assertEqual(view.amount_fate.remainder_minor, 0)
        self.assertFalse(view.originals_at_bank)
        self.assertEqual(view.originals_returned_messages, ("B6",))
        # B3 只接受 plate-transcription；invoice-piece 仍未接受。
        self.assertEqual(view.open_discrepancies, ("invoice-piece",))
        self.assertTrue(view.settlement_blocked)
        # 船代在交单后签发了 rev2（加了 V902、持有人变成银行）。
        self.assertTrue(view.correction_after_presentation)
        self.assertEqual(view.presented_bill_ref, "BL-1@rev1")
        self.assertEqual(view.latest_bill_ref, "BL-1@rev2")
        self.assertEqual(view.approval_ref, "H-14")

    def test_reused_original_group_needs_new_authorization_after_return(self):
        # O-14 已退正本，但 H-14 是单次授权且已被 PR-1 核销。
        decision = self.ledger.evaluate(
            "O-14", "LC-19", 3, "EX-57",
            date.fromisoformat("2026-12-08"), 5_000_000,
        )
        codes = {b.code for b in decision.blockers}
        self.assertIn("HOLDER_PERMISSION_MISSING", codes)
        self.assertNotIn("ORIGINAL_HELD_BY_BANK", codes)

    def test_second_vessel_requires_effective_model_revision(self):
        # rev2 买卖双方都同意但银行未生效；rev3 已生效。
        blocked = self.ledger.evaluate(
            "O-15", "LC-19", 2, "EX-57",
            date.fromisoformat("2026-12-07"), 24_000_000,
        )
        self.assertIn("REV_NOT_EFFECTIVE", {b.code for b in blocked.blockers})

        ok = self.ledger.evaluate(
            "O-15", "LC-19", 3, "EX-57",
            date.fromisoformat("2026-12-07"), 24_000_000,
        )
        self.assertTrue(ok.submittable)
        self.assertEqual(ok.available_minor, 52_000_000)
        self.assertEqual(ok.bill_rev, "BL-2@rev1")

    def test_cashier_line_separates_fee_receipt_and_pending(self):
        line = self.ledger.cashier_view("LC-19")
        self.assertEqual(line.effective_revision, 3)
        self.assertEqual(line.limit_minor, 52_000_000)
        self.assertEqual(line.occupied_minor, 0)
        self.assertEqual(line.available_minor, 52_000_000)
        self.assertEqual(line.claimable_receipt_minor, 17_000_000)
        self.assertEqual(line.fee_minor, 22_500)
        self.assertEqual(line.net_receipt_minor, 17_000_000 - 22_500)

    def test_credit_overview_lists_effectiveness_gap(self):
        overview = self.ledger.credit_overview("LC-19")
        rev2 = next(r for r in overview["revisions"] if r["revision"] == 2)
        self.assertFalse(rev2["effective"])
        self.assertEqual(rev2["missing"], ["缺开证行生效通知"])


class CliSmokeTest(unittest.TestCase):
    def test_evaluate_and_presentation_commands_emit_json(self):
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            code = cli_main([
                "--samples", str(SAMPLES), "evaluate",
                "--group", "O-15", "--credit", "LC-19", "--revision", "3",
                "--presenter", "EX-57", "--date", "2026-12-07", "--minor", "24000000",
            ])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["可递交"])
        self.assertEqual(payload["信用证版本"], "LC-19@rev3")

        buf = io.StringIO()
        with redirect_stdout(buf):
            cli_main(["--samples", str(SAMPLES), "presentation", "PR-1"])
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["交单编号"], "PR-1")
        self.assertEqual(payload["金额_最小单位"]["银行费"], 22500)


if __name__ == "__main__":
    unittest.main()
