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

---

# 阶段三：LangGraph 编排层（Agent Hub 基础架构）

把「单 Agent 广告诊断」升级成「统一入口 + 意图路由 + 专业 Agent 注册 + 工作流编排 + HTTP API」。
**原有同步层、工具层、审批链一行未改**。

## 分层

```
FastAPI API 层          hub/api/agent.py        POST /api/v1/agent/chat
        ↓
LangGraph 编排层        hub/orchestration/      understand → route → dispatch → [approval_gate] → aggregate
        ↓
专业 Agent 层           hub/orchestration/agents/  advertising（已实现）/ product·inventory·pricing（占位）
        ↓
现有业务能力            hub/agent/              tools / agent / **approval（唯一写入口）**
        ↓
Domain Services         hub/domain/              ACOS / ROAS / CTR / CVR / 阈值判断（纯函数）
Platform Adapters       hub/adapters/            local（内存）/ mock（mock_amazon）/ 真实（未接入）
        ↓
基础设施                hub/db.py, hub/sync/     SQLite·PostgreSQL / Redis 限流
```

编排层**不写数据库业务逻辑、不做审批、不做平台写操作**。写操作唯一入口仍是 hub/agent/approval.py::execute()。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/v1/agent/chat | 统一入口；带 pproval + 	hread_id 时是审批恢复 |
| GET | /api/v1/agent/runs/{thread_id} | 查询某次运行的状态（含待审批、next 节点） |
| GET | /api/v1/agent/capabilities | 能力清单，**明确标注哪些 Agent 未实现** |

认证：Authorization: Bearer <token>。**请求体不允许传 shop_id**（extra="forbid"，传了直接 422）——
店铺只能由后端 token 映射决定，防止越权操作别人的店。

`ash
# 1) 发起一次运行
curl -X POST http://localhost:8000/api/v1/agent/chat \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{"message":"广告花费太高，关键词出价要降"}'
# → status=awaiting_approval，payload.pending_approvals 里是待审批建议

# 2) 用同一个 thread_id 审批（appove 后才会调 approval.execute）
curl -X POST http://localhost:8000/api/v1/agent/chat \
  -H "Authorization: Bearer dev-token" -H "Content-Type: application/json" \
  -d '{"message":"同意","thread_id":"<上一步的 thread_id>",
       "approval":[{"suggestion_id":"<sid>","decision":"approve"}]}'
`

## 环境变量（新增）

| 变量 | 默认 | 说明 |
|---|---|---|
| GRAPH_CHECKPOINT_DB | ./hub_checkpoints.sqlite | 工作流状态持久化（独立库，不动业务库） |
| LLM_MODE | ake | ake=内置脚本模型；
eal 需自行注入 |
| ADS_SEED_BIDS | {"S1":{"kw1":1.0,"kw2":0.8}} | local adapter 的种子出价 |
| API_TOKENS | 空 | {"tok":{"shop_id","actor","role"}}，为空时用开发 token |
| DEFAULT_API_TOKEN | dev-token | 开发用 token |
| DEFAULT_SHOP_ID / DEFAULT_ACTOR / DEFAULT_ROLE | S1 / dev / dmin | 开发 token 对应身份 |

## 运行

`ash
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q          # 期望 91 passed
.\.venv\Scripts\python.exe -m uvicorn hub.main:app --reload
`

## 已知边界（本阶段，如实说明）

- Advertising Agent 仍用 FakeLLM；API 响应里 model_mode="fake" 是显式标注的。
- product / inventory / pricing 三个 Agent **只有注册位与占位实现**，调用返回 
ot_implemented，不做任何业务操作。
- 广告平台仍是 InMemoryPlatform（local adapter）；mock_amazon 是自建模拟服务，**不是 SP-API**。
- 真实 Amazon Ads API / SP-API 只有接口骨架，构造即抛 AdapterNotConfigured / AdapterNotImplemented，**没有伪装已接通**。
- 未接入 Playwright RPA；画像里的 RPA 引擎属于后续阶段。
- 单测覆盖编排、API、审批恢复、跨重启持久化；未做压测与多实例并发验证。


---

# 第三阶段补充：本地业务数据接入（无真实 Amazon 授权）

目标：在没有 SP-API / Ads API 授权的前提下，让项目具备**可演示的 Amazon 运营数据查询与分析能力**。
所有数据都是本地文件，**不连接任何真实 Amazon 接口，也不执行任何写操作**。

## 数据来源（三类，严格区分）

| 类型 | 位置 | 说明 | 能否当真实数据用 |
|---|---|---|---|
| **官方 Sandbox 示例** | `data/amazon/fixtures/spapi/` | 从 Amazon 官方 Swagger 模型的 `x-amzn-api-sandbox.static` 抽取的公开示例，用于字段结构对齐 | ❌ 不是真实卖家数据 |
| **Synthetic（合成业务数据）** | `data/amazon/*.json` | 我们自造的、严格自洽的业务数据（商品/订单/库存/价格/广告） | ❌ 不是真实数据，非实时库存 |
| **场景覆盖** | `data/amazon/scenarios/*.json` | 在 synthetic 基础上的内存覆盖（不改磁盘），用于演示异常场景 | ❌ 仅演示 |

官方文档：<https://developer-docs.amazon/sp-api/docs/sp-api-sandbox>
（Static = Swagger 里写死的 mock；Dynamic = 后端按参数返回。FBA Inventory 官方只有 Dynamic，无 static 示例。）

> 公开商品评论数据集（McAuley Lab）实际可达，但 Electronics 元数据为 **1.31 GB**，
> 按"不下载大规模数据"的要求未下载，也没有把它的字段伪装成真实卖家数据。

## 目录

```
data/amazon/
  products.json     商品（ASIN/SKU/标题/品牌/价格/币种/来源）
  orders.json       订单（订单号/ASIN/SKU/数量/金额/状态/时间）
  inventory.json    库存（在库/预留/在途，available 由 adapter 推导）
  pricing.json      价格（本店价/竞品价/是否 buybox）
  advertising.json  广告（曝光/点击/订单/花费/销售额，按关键词聚合）
  scenarios/        normal / low_inventory / order_spike / advertising_drop / price_anomaly
  fixtures/spapi/   官方 Sandbox 示例（orders/catalog/pricing/reports + FBA inventory 说明）
```

一致性由 `tools/gen_local_data.py` 强制校验：订单 ASIN/SKU 必须存在、金额 = 数量 × 单价、
库存 `on_hand >= 0`、`reserved <= on_hand`、广告/价格的 ASIN 必须存在。

## 读取 Adapter

`hub/adapters/local_data.py::LocalDataAdapter`（只读，无写方法）：

- `products(shop_id, asin=, sku=)`
- `product(shop_id, asin= | sku=)`
- `orders(shop_id, status=, asin=, sku=, since=, limit=)`
- `order_stats(shop_id, days=)`
- `inventory(shop_id, sku=, asin=, low_only=)`
- `inventory_risk(shop_id)` → 可售天数 + critical/warn/ok
- `prices(shop_id, asin=)` / `price_anomalies(shop_id, deviation=)`
- `advertising(shop_id, asin=)` / `ad_performance(shop_id, asin=)`（复用 domain 的 ACOS/ROAS/CTR/CVR）

错误码：`file_missing` / `invalid_arg` / `not_found` / `scenario_not_found`。

## 只读 Agent（接入现有 Registry）

| Agent | 意图 | 能力 |
|---|---|---|
| `ProductAgent` | product | 商品与价格查询 |
| `InventoryAgent` | inventory | 库存查询、缺货风险（可售天数） |
| `OrdersAgent` | order | 最近订单、按状态过滤、7 天统计 |
| `AdPerformanceAgent` | ad_performance | 广告表现（ACOS/ROAS/CTR/CVR + 阈值） |
| `OverviewAgent` | overview | 综合运营巡检（库存+订单+广告+价格） |
| `AdvertisingAgent` | advertising | 原有诊断与调价建议（写操作仍走审批链） |
| Pricing Agent | pricing | **仍未实现**（写操作），调用返回 `not_implemented` |

## 只读 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/amazon/products?asin=&sku=` | 商品 |
| GET | `/api/v1/amazon/orders?status=&limit=` | 订单 + 7 天统计 |
| GET | `/api/v1/amazon/inventory?sku=&low_only=` | 库存 + 风险 |
| GET | `/api/v1/amazon/advertising?asin=` | 广告 + 表现评估 |
| GET | `/api/v1/amazon/summary` | 综合只读汇总 |

全部要求 `Authorization: Bearer <token>`；`shop_id` 只来自 token 映射；响应带 `source: "synthetic"`。

## 演示与数据重建

```bash
python tools/gen_local_data.py     # 重新生成并校验本地数据
python tools/demo_ops.py           # 跑 5 种业务场景（走真实 LangGraph）
python -m pytest -q                # 178 passed / 2 skipped
```
