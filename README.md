# 多次装船的信用证交单与正本权利

海运正本、银行审单和信用证额度以及回款。

`samples/` 保存合成的原始交换样本和字段约定，《交换资料约定》说明同一对象的
编号、时间及来源。`lcwork/` 是在这些 feed 之上的业务处理服务，让单证组可持续
办理装船单据、审单与分批收款，并让任何复核人都能看清“这套材料为什么还能交、
或缺的是哪一份授权”。数据均为合成。

## 运行环境

使用 Python 3.11.2，仅依赖标准库，无需下载安装包。由项目根目录运行：

```sh
python3 -m unittest discover -s tests -v
```

测试在 `python -I` 隔离模式下同样可运行（测试文件按自身位置自举项目路径），
例如 `python3 -I -m unittest discover -s "$PWD/tests"`。

## 业务规则落点

| 业务要求 | 实现位置与行为 |
| --- | --- |
| 同一正本组只能用于一次有效交单；复制件/补印件不新增正本 | `Ledger._presentations_holding_group`：同组有交单且未收到 `originals_returned` 即整组锁定，新版提单沿用同组不解锁 |
| 复核员仅在持有人已授权、实装内容与证条件一致时放行 | `Ledger.evaluate` 依次核对正本位置、持有人授权、修订生效、分批、装船/交单日期边界（边界当天可用）、车型、剩余额度；每条带证据与原因码 |
| 授权为持有人对交单人的单次授权 | `Ledger._usable_permission` + 交单索引按交付顺序核销授权；退单后重交必须补办新授权 |
| 交单后船代更正：未决审单挂起等退单证据；已议付列更正待处理并保留收款 | `presentation_view` 的 `correction_status`：`suspended_pending_return` / `negotiated_pending_correction`，递交时版本与最新版本同时展示，原交正本不被改写 |
| 修订需买卖双方同意且收到开证行生效通知 | `CreditRev.effective` 与 `missing_effectiveness`，未生效版本给出缺的具体要件（买方/卖方同意、银行通知） |
| 已递交交单按银行确认的版本为准，不用新版重判 | 交单视图固定使用消息中的 `(credit, revision)`；新版条款只影响新的 `evaluate` |
| waiver 只覆盖列明不符点；未接受差异阻断结算 | 进件时校验 waiver 范围；视图区分列明/已接受/未接受不符点，`settlement_blocked` 独立于是否已收款 |
| 退单前原金额与正本不得再投入；一份回话丢失不挡其他有余量的交单 | 金额占用按“票面 − 已议付 − 已释放”逐笔计算，正本按组独立锁定；见 `held_amount`、`occupied_by_credit` |
| 部分议付的剩余金额给具体结论与依据 | `AmountFate`：有 `amount_released` → 可重新交；无结论 → 等退单/释放通知，附消息编号 |
| 银行费与应收分开看 | 出纳视图与交单视图分列票面、已议付、银行费、净收款 |
| 进件格式严格 | `lcwork/loader.py` 校验字段、引用、消息顺序、票面金额、waiver 范围，异常抛 `FeedError` |

## 查询命令

所有命令输出 UTF-8 JSON，金额均为币种最小单位。

```sh
# 全部信用证概览（版本生效情况、交单、出纳）
python3 -m lcwork.cli credits

# 单个信用证：各修订版缺效要件、占用中的正本组、出纳行
python3 -m lcwork.cli credit LC-19

# 出纳：当前可用余额、可认领实收、银行费、悬置款
python3 -m lcwork.cli cashier LC-19

# 单次交单全过程：用的哪版条款、哪些正本在银行、不符点接受范围、下一步谁办
python3 -m lcwork.cli presentation PR-1

# 拟递交交单的放行判定（不可递交时逐条列出阻断原因与证据）
python3 -m lcwork.cli evaluate \
  --group O-15 --credit LC-19 --revision 3 \
  --presenter EX-57 --date 2026-12-07 --minor 24000000
```

## 代码结构

- `lcwork/model.py`：领域对象与只读索引（信用证版本、提单版本、银行消息、授权）。
- `lcwork/loader.py`：JSONL 进件、格式/引用/顺序的严格校验。
- `lcwork/engine.py`：交单判定、额度占用、不符点/退单/更正状态机、出纳与交单视图。
- `lcwork/serializers.py`：面向业务人员的中文 JSON 结构。
- `lcwork/cli.py`：命令行入口。
- `tests/`：进件校验、规则单测与基于自带合成资料的端到端断言。
