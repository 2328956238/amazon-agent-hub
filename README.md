# 跨境运营 Agent 中台（亚马逊运营场景）· 阶段一 + 阶段二

模拟亚马逊服务 + 数据同步层（阶段一）+ 运营诊断 Agent 与审批链（阶段二）。

> 对应简历项目：**跨境运营 Agent（多 Agent 协同）**。当前实现为单诊断 Agent + 审批链，多 Agent 拆分（诊断 / 审批 / 执行 + 编排器）在推进中。

核心原则：**模型只能提建议，建议必须过后端规则和审批，才会真正改线上数据。**

## 目录

- `mock_amazon/`：模拟亚马逊接口（限流、事件推送、重复/丢失故障注入）
- `hub/sync/`：阶段一同步层（事件去重、防乱序订单写入、Redis 分布式令牌桶、补拉水位线）
- `hub/agent/`：阶段二诊断 Agent 与审批链
  - `tools.py`：工具白名单 + Pydantic 参数校验 + 后端注入 `shop_id`（店铺隔离）
  - `llm.py`：`FakeLLM`（按脚本回答，离线测试用；真实模型实现同样的 `next()` 即可替换）
  - `agent.py`：诊断循环（步数/工具调用次数上限、token 统计）+ 证据校验
  - `platform.py`：`InMemoryPlatform` 内存版广告平台（可替换成 HTTP 实现）
  - `approval.py`：规则校验、风险分流、角色审批、条件更新状态机、执行/回滚/审计
  - `pipeline.py`：诊断 → 证据校验 → 规则校验与分流的串行链路
- `hub/tests/`：22 个测试（阶段一 6 个 + 阶段二 16 个）
- `demo_phase2.py`：阶段二全链路演示（打印每一步实际结果）

## 环境

- Python 3.12（已在本机验证）
- 依赖见 `requirements.txt`（fastapi / sqlalchemy[asyncio] / aiosqlite / asyncpg / redis / httpx / pydantic v2 / pytest / pytest-asyncio / fakeredis[lua]）

## 运行

```powershell
cd amazon-agent-hub
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe demo_phase2.py
```

预期：`22 passed`。演示会依次打印：工具越权被拒、死循环被终止、完整审批执行回滚链路与审计日志、
编造证据被丢弃、离谱出价被规则拦截、20 个并发执行只生效 1 次、20 个并发审批只成功 1 次、
审批期间被改价时执行失败且不覆盖。

## 阶段二测试覆盖

| 层 | 做法 | 对应测试 |
| --- | --- | --- |
| 工具白名单 | 只有登记过的工具能被调用，名单外直接拒绝并记日志 | `test_whitelist_and_args_validation`、`test_forbidden_tool_is_logged_not_executed` |
| 参数校验与店铺隔离 | 禁止多余字段；`shop_id` 由后端注入，模型传 `shop_id` 被拒 | `test_whitelist_and_args_validation`、`test_shop_isolation` |
| 防死循环/成本 | 最大步数 + 最大工具调用次数 + token 用量记录 | `test_loop_limits`、`test_token_cost_recorded` |
| 证据校验 | 建议必须引用本次运行里真实存在的工具结果，数值要对得上 | `test_valid_evidence_creates_suggestion`、`test_fabricated_evidence_dropped` |
| 规则校验 | 出价下限、单次调幅上限、每日改动次数、建议旧值是否过期 | `test_bad_model_output_blocked_by_rules`、`test_rules` |
| 风险分流与角色 | 低风险自动通过；中风险需人工；高风险只有管理员能批 | `test_risk_routing_and_roles` |
| 状态机 | 带条件的 UPDATE 做状态流转，并发下只有一个调用者成功 | `test_concurrent_approve_only_one_wins`、`test_concurrent_execute_applies_once` |
| 执行与回滚 | 执行前再核对旧值；失败后不重复执行；保留修改前快照可回滚 | `test_execute_failure_is_final_and_safe`、`test_stale_at_execute_time`、`test_rollback_restores_before_value` |
| 审计 | 每一步记录操作人、结果和原因，顺序与内容逐条断言 | `test_audit_trail` |

## 关键设计

### 阶段一

- **事件去重**：`platform_event` 表 `(shop_id, event_id)` 唯一约束 + `ON CONFLICT DO NOTHING`。
- **防乱序**：订单 UPSERT 带 `WHERE orders.last_update_time < excluded.last_update_time`，旧事件不覆盖新状态；
  去重和订单更新在同一个事务里。
- **Redis 令牌桶**：Lua 脚本保证「读-算-写」原子，多进程共享一份配额；`Retry-After` 优先，否则指数退避。
- **补拉水位线**：独立 `sync_cursor` 表，仅在一批数据全部写成功后随同一事务推进，并回退 5 分钟重叠窗口防边界漏数据。

### 阶段二

- **模型只提建议**：Agent 循环只负责调工具和产出 `suggestions`，落库前必须通过证据校验与规则校验。
- **证据校验**：按 `run_id + call_id` 查本次运行的工具调用日志，核对 `keyword_id/field/value`，
  编造数值或不存在的调用编号直接被丢弃，不进入审批。
- **规则校验**：出价下限、单次调幅上限、每日改动次数、`before` 是否等于平台当前值，全部由后端代码判断。
- **风险分流**：调幅 ≤10% 低风险自动通过；≤20% 中风险需人工；>20% 高风险只有管理员能批。
- **状态机**：`UPDATE suggestion SET status=:to WHERE id=:id AND status=:from`，`rowcount == 1` 才算抢到；
  20 个并发审批只有 1 个成功，20 个并发执行平台只被调用 1 次。
- **执行安全**：`APPROVED -> EXECUTING` 抢占执行权；执行前重新读取平台出价，与建议的 `before` 不一致则失败；
  平台调用带幂等键 `suggestion.id`；失败后状态为 `FAILED`，不会被重复执行。
- **回滚**：`suggestion.before_value` 是修改前快照，回滚按 `EXECUTED -> ROLLING_BACK -> ROLLED_BACK` 流转。
- **审计**：`approval_log` 记录每一步的 `step/actor/result/detail`。

## 已知边界（如实说明）

- 全程使用 `FakeLLM`（按脚本回答），**没有接过真实大模型**，因此无法量化诊断准确率；接真实模型后需要评测集。
- 广告平台是内存版 `InMemoryPlatform`，**尚未接到模拟亚马逊的 HTTP 接口**，也没有审批 API 和页面。
- 数据库是 SQLite，**未在真实 PostgreSQL 上验证**。
- 「每天改动次数」检查在并发提交时不是原子的：两个请求同时提交可能都通过检查。真实上线需要加锁或唯一约束。
- 「旧值过期」采用读后比较，平台侧没有版本号/CAS；高并发下最坏情况仍可能覆盖别人刚做的修改（测试覆盖的是串行与常见并发窗口）。
- 未做吞吐 / P99 压测、熔断、死信表、多店铺鉴权与凭证加密。
