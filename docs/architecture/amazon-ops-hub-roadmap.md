# Amazon Agent Hub 升级蓝图

> 目标：把当前广告诊断原型升级为 Python 后端主导、LangGraph 编排的 Amazon 智能运营系统。本文是实施基线，不代表下列模块已经全部实现或已经接入 Amazon 生产 API。

## 1. 产品范围

统一自然语言入口：运营主 Agent（Supervisor）接收任务、识别店铺与业务域、拆解任务、调用专业能力并汇总结果。

专业业务域：
- 商品管理：商品草稿、Listing 创建/编辑、属性与图片资料管理、上架状态同步。
- 库存优化：库存/销量/在途量汇总、缺货风险、补货建议、滞销预警。
- 广告策略：活动/广告组/关键词指标分析，ACOS、ROAS、CTR、CVR 诊断，预算与竞价建议。
- 定价自动化：成本、费用、最低利润线、价格边界与竞品参考，生成调价建议。
- RPA 执行：仅作为 API 不支持或必须经过卖家后台页面的补充执行通道。

## 2. 目标架构

```text
FastAPI
  ├── API / Auth / Tenant-Shop Context
  ├── LangGraph Supervisor
  │     ├── Product Agent
  │     ├── Inventory Agent
  │     ├── Advertising Agent
  │     └── Pricing Agent
  ├── Domain Services（普通 Python，不依赖 LangGraph）
  │     ├── product / inventory / advertising / pricing
  │     └── policy / approval / execution / audit
  ├── Platform Adapters
  │     ├── Amazon SP-API adapter
  │     ├── Amazon Ads API adapter
  │     ├── Mock adapters（开发与离线演示）
  │     └── Browser RPA adapter（受控补充通道）
  └── Infrastructure
        ├── PostgreSQL（持久化）
        ├── Redis（缓存、限流、任务协调）
        ├── Background workers
        └── Observability
```

LangGraph 只负责状态流转、路由、重试边界和人工审批中断；商品、库存、广告、定价的业务规则放在独立 Python Service 中，便于单测、复用和非 Agent API 调用。

## 3. 统一执行原则

1. 读操作与写操作分离；Agent 默认只能读取和提出建议。
2. 所有写操作必须通过后端登记的 Action Registry，不允许模型任意指定 URL、SQL、浏览器脚本或工具名。
3. 写操作经过 Pydantic 参数校验、店铺权限校验、业务规则校验、风险分级和审批。
4. 每个写操作保存 before/after 快照、幂等键、执行状态和审计记录；执行前再次读取远端当前值，发现冲突则停止。
5. API 优先；只有 API 不覆盖且平台规则允许时才使用 RPA。RPA 不绕过验证码、MFA、访问控制或平台限制。
6. Mock 数据和真实平台数据必须有明确来源标记；不得把 Mock 成功描述成 Amazon 已真实执行。
7. 凭证加密存储，日志脱敏；所有查询和动作都必须绑定后端注入的 tenant_id / shop_id。
8. 对限流、超时、网络错误做有界重试与退避；对非幂等写操作不得盲目重试。

## 4. 适配器边界

统一定义平台能力接口，业务服务不直接依赖 HTTP 或浏览器实现。至少区分：
- Catalog/Listings：读取目录与 Listing、创建/更新 Listing、提交图片/媒体资料、读取处理状态。
- Inventory：读取可售库存、入库/预留等平台库存状态。
- Pricing：读取报价/价格，提交允许的价格更新。
- Advertising：读取报告与实体，提交允许的预算/竞价变更。
- RPA：通过显式授权的浏览器会话执行有限的 UI 工作流，并返回截图/页面状态/步骤审计。

先实现确定性的 Mock Adapter 与契约测试，再接真实 API。Amazon API 的可用操作、字段、地区和权限必须以当期官方文档及应用授权为准，不能假设每个 Seller Central 页面操作都有公开 API。

## 5. 数据域建议

在现有模型上逐步迁移，不一次性重建数据库：
- Shop / PlatformCredential（店铺与授权凭证元数据）
- Product / Listing / ListingAttribute / ListingMedia
- InventorySnapshot / SalesVelocity / ReplenishmentRecommendation
- AdCampaign / AdGroup / KeywordMetric / AdRecommendation
- PricePolicy / PriceSnapshot / PriceRecommendation
- Operation / Approval / ExecutionAttempt / AuditEvent
- SyncCursor / PlatformEvent（沿用现有同步去重与水位线思路）

所有业务表按店铺隔离；外部平台 ID 与内部 ID 分开存储；对时间、币种、市场站点和单位显式建模。

## 6. 实施顺序与验收边界

### Phase 0：基线与工程化
- 保留现有广告诊断、证据校验、审批状态机、幂等执行和审计能力。
- 补齐配置管理、数据库迁移、日志与 API 错误模型。
- 把 SQLite 仅作为快速测试选项；以 PostgreSQL 作为目标数据库进行兼容性验证。

### Phase 1：平台接入骨架
- 定义 SP-API / Ads API 的接口与 DTO。
- Mock Adapter 可完整演示；真实 Adapter 需要开发者应用、卖家授权、区域端点和所需角色权限。
- 同步任务支持分页、限流、断点续传、去重和失败重试。

### Phase 2：商品管理
- 商品查询、草稿创建、Listing 属性校验、编辑预览、提交、处理状态轮询。
- 商品写操作先走审批；图片先做素材校验与存储引用，不伪造上传成功。
- 真实 Listing API 能力以目标站点和商品类型的官方要求为准。

### Phase 3：库存优化
- 可售/预留/在途库存与销量窗口汇总。
- 缺货预测、补货量建议、滞销预警。
- 第一版只生成建议与通知；自动采购或库存写操作另设审批策略。

### Phase 4：广告策略
- 将现有关键词诊断抽象为广告业务服务。
- 补充活动、广告组、搜索词/关键词指标与预算分析。
- 建议进入统一审批与执行流水线。

### Phase 5：定价自动化
- 建立成本与费用输入、最低利润线、价格上下限。
- 先做价格建议与冲突检测，再开放经审批的价格更新。
- 竞品价格数据必须注明来源与时效，不假设 API 一定提供。

### Phase 6：LangGraph 与运营主 Agent
- Typed State、Supervisor 路由、子图/节点、最大步骤与工具预算。
- 高风险写操作使用 interrupt / human approval；恢复执行必须复用同一 operation_id。
- Agent 负责规划和解释，Service 与 Policy 负责事实、规则和执行。

### Phase 7：RPA 补充通道
- 先选一个低风险、可验证的卖家后台流程做 PoC。
- Playwright 使用专用授权会话、最小权限、显式人工确认；页面变化或验证失败立即停止。
- 不存储明文密码，不绕过 MFA/CAPTCHA，不做批量高频 UI 操作。

### Phase 8：交付与演示
- 提供本地 Docker Compose、环境变量模板、数据库迁移、API 文档和可重复 Demo。
- 演示数据明确标注 Mock；真实接入需要用户自行完成平台授权。
- 验收覆盖店铺隔离、权限、审批、幂等、并发、失败恢复、审计与数据来源。

## 7. 当前仓库已确认的事实

根据当前 main 分支 README 与源码：
- 已有事件同步/去重设计、广告诊断 Agent、工具白名单与 Pydantic 参数校验。
- 已有证据校验、风险分流、审批状态机、幂等执行、回滚快照和审计日志。
- 当前 Agent 使用 FakeLLM；广告执行平台是 InMemoryPlatform。
- 当前库存工具只有按 SKU 查询数量；没有完整库存优化业务。
- 依赖包含 SQLAlchemy async、SQLite/PostgreSQL 驱动、Redis、FastAPI；README 声明当前数据库验证仍以 SQLite 为主。
- 当前没有已验证的真实 SP-API / Ads API 生产连接，没有商品上架/编辑服务，没有 LangGraph 编排，也没有 RPA 执行器。

因此，后续每个模块都必须明确标记为 mock、sandbox 或 production，并用实际接口响应证明真实接入。
