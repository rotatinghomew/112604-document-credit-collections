"""把引擎结论转成稳定的 JSON 可序列化结构，文案面向单证组与出纳。"""

from .engine import (
    CashierLine,
    CheckItem,
    Decision,
    PresentationView,
)

_CHECK_NAMES = {
    "正本组资料": "正本组资料",
    "正本位置": "正本位置",
    "持有人授权": "持有人授权",
    "信用证版本": "信用证版本",
    "修订生效": "修订生效",
    "分批许可": "分批许可",
    "装船日期边界": "装船日期边界",
    "交单日期边界": "交单日期边界",
    "车型一致": "车型一致",
    "剩余额度": "剩余额度",
}

_STATUS_LABELS = {
    "under_examination": "审单中",
    "discrepancy_open": "不符点未决（不可结算）",
    "discrepancy_waived": "不符点已全部接受",
    "paid_with_open_discrepancies": "已议付但存在未接受不符点（合规阻断）",
    "negotiated_partial": "部分议付",
    "negotiated_full": "全额议付",
    "negotiated_correction_pending": "已议付，更正待处理",
    "correction_suspended": "更正挂起，等退单证据",
    "amount_released_pending": "部分金额已释放",
    "returned_unpaid": "退单未议付",
    "closed_returned": "已结清并退正本",
}

_CORRECTION_LABELS = {
    "none": "无交单后更正",
    "suspended_pending_return": "未决审单挂起，等银行退单证据",
    "negotiated_pending_correction": "已议付，更正版待处理（收款记录保留）",
}

_APPROVAL_ISSUE_LABELS = {
    "none": "授权齐全",
    "missing": "找不到匹配的未用持有人授权",
    "reused": "授权被多次使用",
}


def decision_to_dict(decision: Decision) -> dict:
    return {
        "可递交": decision.submittable,
        "信用证版本": decision.credit_rev,
        "提单版本": decision.bill_rev,
        "当前可用余额_最小单位": decision.available_minor,
        "本次拟占用_最小单位": decision.would_occupy_minor,
        "阻断": [
            {
                "代码": b.code,
                "原因": b.reason,
                "证据": list(b.evidence),
            }
            for b in decision.blockers
        ],
        "核对明细": [_check_row(c) for c in decision.checks],
    }


def _check_row(c: CheckItem) -> dict:
    row = {
        "核对项": _CHECK_NAMES.get(c.name, c.name),
        "通过": c.ok,
        "说明": c.detail,
        "证据": list(c.evidence),
        "是否阻断": c.blocking,
    }
    if c.blocking:  # 通过项不挂原因码，只有阻断项需要机器读取的代码
        row["原因码"] = c.code
    return row


def presentation_to_dict(view: PresentationView) -> dict:
    return {
        "交单编号": view.presentation,
        "信用证": view.credit,
        "适用版本": view.credit_ref,
        "正本组": view.original_group,
        "状态": _STATUS_LABELS.get(view.status, view.status),
        "状态说明": view.status_detail,
        "递交时提单版本": view.presented_bill_ref,
        "船代最新提单版本": view.latest_bill_ref,
        "交单后更正": view.correction_after_presentation,
        "更正处理": _CORRECTION_LABELS[view.correction_status],
        "授权编号": view.approval_ref,
        "授权状态": _APPROVAL_ISSUE_LABELS.get(view.approval_issue, view.approval_issue),
        "金额_最小单位": {
            "票面": view.face_minor,
            "已议付": view.paid_minor,
            "已释放可重交": view.released_minor,
            "银行悬置": view.held_minor,
            "银行费": view.fee_minor,
            "净收款_议付减费": view.net_receipt_minor,
        },
        "剩余金额结论": {
            "剩余_最小单位": view.amount_fate.remainder_minor,
            "结论": view.amount_fate.conclusion,
            "依据消息": list(view.amount_fate.basis),
        },
        "正本": {
            "是否在银行": view.originals_at_bank,
            "退单证据消息": list(view.originals_returned_messages),
        },
        "不符点": {
            "银行列明": list(view.listed_discrepancies),
            "买方已接受": list(view.waived_discrepancies),
            "未接受_阻断结算": list(view.open_discrepancies),
            "合规是否阻断结算": view.settlement_blocked,
            "接受依据消息": list(view.waiver_messages),
        },
        "收款消息": list(view.paid_messages),
        "消息链路": list(view.message_chain),
        "下一步": view.next_action,
        "下一步办理人": view.next_owner,
    }


def cashier_to_dict(line: CashierLine) -> dict:
    return {
        "信用证": line.credit,
        "币种": line.currency,
        "生效版本": line.effective_revision,
        "额度_最小单位": line.limit_minor,
        "在途占用_最小单位": line.occupied_minor,
        "可用余额_最小单位": line.available_minor,
        "可认领实收_最小单位": line.claimable_receipt_minor,
        "银行费_最小单位": line.fee_minor,
        "实收净额_最小单位": line.net_receipt_minor,
        "悬而未决款项_最小单位": line.pending_minor,
    }


def overview_to_dict(overview: dict) -> dict:
    return {
        "信用证": overview["credit"],
        "版本": [
            {
                "版本号": row["revision"],
                "已生效": row["effective"],
                "开证行通知": row["bank_effective"],
                "缺效要件": row["missing"],
                "额度_最小单位": row["limit_minor"],
                "允许分批": row["partial"],
                "最迟装船": row["latest_ship"],
                "最迟交单": row["latest_present"],
                "车型": row["models"],
            }
            for row in overview["revisions"]
        ],
        "证下交单": overview["presentations"],
        "正本仍在银行的正本组": overview["occupied_groups"],
        "出纳": cashier_to_dict(overview["cashier_line"]),
    }
