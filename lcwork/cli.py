"""命令行查询入口：所有输出为 UTF-8 JSON，中文键名，便于直接交接核对。

示例：
    python -m lcwork.cli --samples samples credit LC-19
    python -m lcwork.cli --samples samples presentation PR-1
    python -m lcwork.cli --samples samples evaluate \
        --group O-15 --credit LC-19 --revision 3 \
        --presenter EX-57 --date 2026-12-07 --minor 24000000
"""

import argparse
import json
from datetime import date
from pathlib import Path
import sys

if __package__ in (None, ""):  # 支持 python -I lcwork/cli.py 脚本方式运行
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lcwork.engine import Ledger  # noqa: E402
from lcwork.loader import FeedError, load_repository  # noqa: E402
from lcwork.serializers import (  # noqa: E402
    cashier_to_dict,
    decision_to_dict,
    overview_to_dict,
    presentation_to_dict,
)

_DEFAULT_SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def _print(payload: dict | list) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _ledger(args: argparse.Namespace) -> Ledger:
    return Ledger(load_repository(args.samples))


def _cmd_credits(ledger: Ledger, _args: argparse.Namespace) -> None:
    rows = []
    for credit_id in ledger.repo.all_credits():
        overview = ledger.credit_overview(credit_id)
        payload = overview_to_dict(overview)
        rows.append(
            {
                "信用证": payload["信用证"],
                "版本数": len(payload["版本"]),
                "生效版本": [
                    row["版本号"] for row in payload["版本"] if row["已生效"]
                ],
                "交单": payload["证下交单"],
                "出纳": payload["出纳"],
            }
        )
    _print(rows)


def _cmd_credit(ledger: Ledger, args: argparse.Namespace) -> None:
    if not ledger.repo.credit_revisions(args.credit):
        raise SystemExit(f"未知信用证：{args.credit}")
    _print(overview_to_dict(ledger.credit_overview(args.credit)))


def _cmd_cashier(ledger: Ledger, args: argparse.Namespace) -> None:
    if not ledger.repo.credit_revisions(args.credit):
        raise SystemExit(f"未知信用证：{args.credit}")
    _print(cashier_to_dict(ledger.cashier_view(args.credit)))


def _cmd_presentations(ledger: Ledger, _args: argparse.Namespace) -> None:
    _print([presentation_to_dict(v) for v in ledger.all_presentation_views()])


def _cmd_presentation(ledger: Ledger, args: argparse.Namespace) -> None:
    try:
        view = ledger.presentation_view(args.presentation)
    except KeyError as exc:
        raise SystemExit(str(exc)) from exc
    _print(presentation_to_dict(view))


def _cmd_evaluate(ledger: Ledger, args: argparse.Namespace) -> None:
    decision = ledger.evaluate(
        original_group=args.group,
        credit=args.credit,
        revision=args.revision,
        presenter=args.presenter,
        present_date=date.fromisoformat(args.date),
        face_minor=args.minor,
    )
    _print(decision_to_dict(decision))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="信用证交单业务查询服务")
    parser.add_argument("--samples", default=str(_DEFAULT_SAMPLES), help="交换资料目录")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("credits", help="列出全部信用证概览")

    p_credit = sub.add_parser("credit", help="单个信用证的版本、交单与出纳总览")
    p_credit.add_argument("credit")

    p_cashier = sub.add_parser("cashier", help="出纳视图：可用余额、实收、悬置款、费用")
    p_cashier.add_argument("credit")

    sub.add_parser("presentations", help="列出全部交单的全过程视图")

    p_pr = sub.add_parser("presentation", help="单次交单的全过程视图")
    p_pr.add_argument("presentation")

    p_eval = sub.add_parser("evaluate", help="拟递交交单的放行判定")
    p_eval.add_argument("--group", required=True, help="提单正本组号")
    p_eval.add_argument("--credit", required=True)
    p_eval.add_argument("--revision", required=True, type=int)
    p_eval.add_argument("--presenter", required=True, help="交单人（与授权 presenter 对应）")
    p_eval.add_argument("--date", required=True, help="拟交单日 YYYY-MM-DD")
    p_eval.add_argument("--minor", required=True, type=int, help="票面金额（最小单位）")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        ledger = _ledger(args)
    except FeedError as exc:
        raise SystemExit(f"进件资料错误：{exc}") from exc

    handlers = {
        "credits": _cmd_credits,
        "credit": _cmd_credit,
        "cashier": _cmd_cashier,
        "presentations": _cmd_presentations,
        "presentation": _cmd_presentation,
        "evaluate": _cmd_evaluate,
    }
    handlers[args.command](ledger, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
