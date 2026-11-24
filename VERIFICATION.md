# 运行验证记录

本文件记录本机实际运行结果，供核对（不是项目文档，可删除）。

## 环境

| 项目 | 值 |
| --- | --- |
| 日期 | 2026-11-16 21:30 (Asia/Shanghai) |
| 操作系统 | Windows 11 (win32) |
| Python | 3.12.14（使用虚拟环境 `.venv`） |
| pytest | 9.2.0（pytest-asyncio 1.5.0，asyncio_mode=auto） |
| 关键依赖 | fastapi 0.163.1 / SQLAlchemy 2.1.8 / pydantic 2.14.3 / httpx 0.28.1 / redis 8.1.0 / fakeredis 2.40.0 / lupa 2.8 / asyncpg 0.32.0 |
| 安装命令 | `python -m venv .venv` + `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` |

## 测试结果（连续 4 次运行，全部 6 passed）

| 运行 | 结果 | 耗时 |
| --- | --- | --- |
| 第 1 次 | 6 passed, exit 0 | 6.14s |
| 第 2 次 | 6 passed, exit 0 | 5.59s |
| 第 3 次 | 6 passed, exit 0 | 5.83s |
| 第 4 次 | 6 passed, exit 0 | 5.86s |

单测明细：

```
hub/tests/test_chaos.py::test_backfill_recovers_when_all_events_lost PASSED
hub/tests/test_chaos.py::test_chaos_final_consistency PASSED
hub/tests/test_cursor.py::test_incremental_backfill_uses_cursor PASSED
hub/tests/test_sync.py::test_concurrent_duplicate_only_stored_once PASSED
hub/tests/test_sync.py::test_out_of_order_does_not_overwrite_newer PASSED
hub/tests/test_sync.py::test_retry_on_429_respects_retry_after PASSED
```

## 额外验证

用独立脚本复现 `test_incremental_backfill_uses_cursor` 的两次补拉，打印实际数值：

```
first_pull  = 100      # 首次补拉，100 个订单
second_pull = 27       # 第二次补拉（5 分钟重叠窗口内的旧订单会重复拉回）
final_state_matches_source = True
```

与文档中「第二次拉 27 条」一致，最终状态与源端完全一致。

## 旧版补拉 bug 复现（面试故事的事实基础）

把补拉临时改回「用订单表 `MAX(last_update_time)` 当增量起点」的旧实现（其余不变），
在同样的 300 单、重复 30% + 丢失 20% + 乱序场景下实测对比：

| 版本 | 补拉返回 | 库内订单数 | 与源端不一致 |
| --- | --- | --- | --- |
| 旧版（MAX 起点） | 0 条 | 296 | **59 单** |
| 修复版（独立水位线 + 5 分钟重叠） | 300 条（首次全量） | 300 | **0 单** |

旧版采样：`O179`、`O5`、`O182`… 源端 `Shipped`、库内 `Paid`；`O23` 源端 `Shipped`、库内 `Pending`。
根因复现：最新订单把起点推高 → `updated_after=最大时间` 过滤掉全部落后订单 → 补拉返回 0 条，
被丢弃事件的 59 个订单永远停在旧状态。修复后 300 单全部一致。

验证用的临时副本已在验证后删除，正式项目代码未做任何改动。

## 本机环境备注

本机 `%TEMP%` 目录异常：Python 在 `C:\Users\23289\AppData\Local\Temp` 下 `os.open` 会挂起，
`tempfile.gettempdir()` 因此回退到当前工作目录。结果是 pytest 运行后会在项目根留下
`pytest-of-*/` 目录、pip 会留下 `pip-*/` 临时目录，这些都不影响测试结果，已加入 `.gitignore`。
在正常机器上不会出现（临时文件会写到系统 `%TEMP%`）。

## 未验证项（不要写进简历）

- 真实 PostgreSQL（`asyncpg`）与真实 Redis 上的并发行为；
- 吞吐 / P99 等性能数字；
- 熔断、死信表、多店铺隔离、凭证加密、Agent 诊断与审批链（阶段二已于 2026-11-17 实现并验证，见下文）。

---

# 阶段二验证记录（2026-11-17）

## 环境

| 项目 | 值 |
| --- | --- |
| 日期 | 2026-11-17 20:27 (Asia/Shanghai) |
| 操作系统 | Windows 11 (win32) |
| Python | 3.12.14（使用虚拟环境 `.venv`） |
| pytest | 9.2.0（pytest-asyncio 1.5.0，asyncio_mode=auto） |
| 关键依赖 | SQLAlchemy 2.1.8 / pydantic 2.14.3 / FastAPI 0.163.1 / aiosqlite 0.22.1 / fakeredis 2.40.0 |
| 数据库 | SQLite 文件库（WAL 模式，pytest 临时目录） |

## 测试结果：22 passed，连续 5 次稳定

直接对项目源目录连续执行 `.\.venv\Scripts\python.exe -m pytest -q`：

| 运行 | 结果 | pytest 耗时 | 退出码 |
| --- | --- | --- | --- |
| 第 1 次 | 22 passed | 8.20s | 0 |
| 第 2 次 | 22 passed | 8.40s | 0 |
| 第 3 次 | 22 passed | 8.54s | 0 |
| 第 4 次 | 22 passed | 8.83s | 0 |
| 第 5 次 | 22 passed | 8.28s | 0 |

阶段一 6 个 + 阶段二 16 个，测试清单见 README。独立验证员在 `.verify_workspace/clean`
副本（同一 venv）上又连跑 5 次，5/5 均为 22 passed（7.81s–8.63s），另加 1 次全量复跑
22 passed；无 failed / error / warning，无 flaky。

## 演示运行（`demo_phase2.py`）

完整输出见 `demo_phase2_output.txt`，实际输出要点：

```
[1] delete_everything -> tool_not_allowed；模型传 shop_id -> bad_args；days=999 -> bad_args
    S1/kw1 spend=52.2 orders=0，S2 同 keyword_id 的 999.0 不可见
[2] 一直请求工具 -> aborted_too_many_calls，steps=5，工具只执行 4 次，tokens=400
[3] 真实证据建议 1.00 -> 0.70：risk=high，PENDING_APPROVAL
    运营审批 forbidden；管理员审批 approved；执行 executed，出价 0.70；回滚 rolled_back，恢复 1.00
    审计：rule_check/pass -> approve/denied -> approve/ok -> execute_start/ok
          -> execute_done/ok -> rollback/ok
[4] 编造 spend=999.0 或 call_id=c9 -> dropped_no_evidence=1，进入审批 0 条
[5] 真实证据但出价 0.01 -> REJECTED_BY_RULE，violations=[below_min_bid, change_too_large]，平台调用 0 次
[6] 20 个并发 execute -> executed=1，skipped=19，平台实际调用 1 次，出价 1.00 -> 0.92
[7] 20 个并发 approve -> approved=1，conflict=19
[8] 审批期间出价被改成 1.20 -> execute failed，平台保持 1.20，未被覆盖
落库：diagnosis_run=5，tool_call_log=8，suggestion=5，approval_log=55
```

## 变异检验（独立验证员执行，详见 `.verify_workspace/VERIFY_REPORT.md`）

对 clean 副本做 6 处破坏，每处单独 copytree 并确认 diff 只改一处；先确认干净副本目标测试
PASSED，再确认变异副本 FAILED；每处重复 3 次，18/18 稳定失败：

| 变异 | 目标测试 | clean | 变异后 | 关键失败断言 |
| --- | --- | --- | --- | --- |
| M1 状态机去掉 `AND status = :frm` | `test_risk_routing_and_roles` | PASSED | FAILED | reject 返回 rejected ≠ conflict；并发审批 approved=20≠1（次要测试也红） |
| M2 删除 below_min_bid | `test_bad_model_output_blocked_by_rules` | PASSED | FAILED | violations 缺 below_min_bid |
| M3 evidence_ok 首行 return True | `test_fabricated_evidence_dropped` | PASSED | FAILED | outcomes 多 1 条、dropped=0 |
| M4 删除执行前旧值核对 | `test_stale_at_execute_time` | PASSED | FAILED | execute 返回 executed ≠ failed |
| M5 高风险允许 operator | `test_risk_routing_and_roles` | PASSED | FAILED | approve 返回 approved ≠ forbidden |
| M6 删除 `extra="forbid"` | `test_whitelist_and_args_validation` | PASSED | FAILED | DID NOT RAISE ToolError（shop_id 未被拒） |

断言强度抽查：`test_agent.py` 的 16 个测试共 42 条顶层 assert + 3 处 `pytest.raises`；
并发断言 `approved==1` / `conflict==19`、`executed==1` / `skipped==19`、`len(platform.calls)==1`、
执行后 `bid==0.92` 均存在，未发现恒真断言。

## 顺带修复：Retry-After 向下取整

`mock_amazon/main.py` 返回 429 时，`Retry-After` 从四舍五入改为
`max(0.01, math.ceil(info * 100) / 100)`（向上取整且至少 0.01 秒）。实测：

| 实际需等待 | 旧写法 | 新写法 | 旧写法是否少算 |
| --- | --- | --- | --- |
| 0.9949s | 0.99 | 1.00 | 是 |
| 0.0049s | 0.00 | 0.01 | 是 |
| 0.5000s | 0.50 | 0.50 | 否 |
| 0.0000s | 0.00 | 0.01 | 是（旧写法等于不等待） |

真实 429 重试（mock bucket rate=1/burst=1，httpx ASGI transport）：第一次 429，
按 `Retry-After: 1.00` 等待后重试成功，`throttled_429=1`、`retries=1`，总耗时 1.02s。

## 说明与边界

- 全程 FakeLLM + 内存平台，验证的是工程可靠性，不是诊断效果；诊断准确率需要真实模型 + 评测集。
- 仅 SQLite 文件库验证；未在真实 PostgreSQL / Redis 上运行。
- 「每天改动次数」检查在并发提交时不是原子的：两个请求可能同时通过检查，真实上线需要加锁或唯一约束。
- 执行前旧值核对是读后比较，平台侧没有版本号/CAS；极端并发窗口仍可能覆盖别人刚做的修改。
- 未做审批 API 接口和页面，也未接模拟亚马逊的 HTTP 广告平台。
- 验证期间源项目只有 README.md、.gitignore 和演示文件被 Lead 更新；29 个代码文件与验证快照
  SHA-256 一致，测试结论对当前代码成立。
