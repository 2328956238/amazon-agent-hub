"""Mock SP-API Adapter：包装现有 mock_amazon 模拟服务。

必须说清楚的事：
- 它调用的是本仓库自建的 `mock_amazon`（FastAPI 模拟服务），**不是** Amazon 真实 SP-API；
- 返回结构与真实 SP-API 不同，字段是模拟服务自己定的；
- mode=MOCK，API 响应里会带 `mode` 字段，前端/面试演示都能看出来。

复用现有 hub/sync/client.py 的 AmazonClient：它已经带了限流（Redis 令牌桶）、
429 Retry-After、指数退避和失败重试——这正是"Adapter 不重复造轮子"的意义。
"""

from __future__ import annotations

from typing import Any

from ..sync.client import AmazonClient
from .base import AdPlatformMode


class MockSpApiAdapter:
    """把 AmazonClient 的 HTTP 调用包装成 SpApiAdapter 协议。"""

    mode = AdPlatformMode.MOCK

    def __init__(self, client: AmazonClient):
        self.client = client

    async def list_orders(self, shop_id: str, updated_after: str | None = None) -> list[dict[str, Any]]:
        data = await self.client.get("/orders", shop_id, "orders",
                                     **({"updated_after": updated_after} if updated_after else {}))
        return data.get("orders", [])

    async def get_inventory(self, shop_id: str, sku: str) -> dict[str, Any]:
        # mock_amazon 的 /inventory/{sku} 不校验 x-shop-id，用 shop_id 作为限流 key
        data = await self.client.get(f"/inventory/{sku}", shop_id, "inventory")
        return data

    def describe(self) -> dict[str, str]:
        return {
            "mode": self.mode.value,
            "source": "mock_amazon（本仓库自建的模拟服务）",
            "note": "不是 Amazon 真实 SP-API，返回字段与真实接口不同",
        }
