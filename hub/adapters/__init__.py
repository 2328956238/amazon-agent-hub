"""Platform Adapters：把"平台长什么样"和"业务怎么用"隔开。

三种实现必须能一眼分清，不允许把模拟说成真实：

| 模式 | 含义 | 本仓库对应 |
|---|---|---|
| local | 进程内内存实现，仅供测试与演示 | LocalAdsAdapter（包装 InMemoryPlatform） |
| mock  | 本地模拟服务，行为和真实平台不同 | MockSpApiAdapter（包装 mock_amazon） |
| sandbox | 平台官方测试环境（要凭证） | 未接入 |
| production | 真实授权环境（要凭证） | 未接入 |
"""
