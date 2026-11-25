"""Local Ads Adapter：把现有的 InMemoryPlatform 包装成统一 Adapter。

约束（按升级指令 S2）：
- **不修改** hub/agent/platform.py 的任何一行；
- 用组合（composition）而非继承，保留原类的全部行为（calls / applied_keys / fail_next）；
- 明确标注 mode=LOCAL，任何"这是真实平台"的说法都是错的。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..agent.platform import InMemoryPlatform
from .base import AdPlatformMode


@dataclass
class LocalAdsAdapter:
    """内存广告平台适配器。行为与 InMemoryPlatform 完全一致，只是多了一层统一接口。"""

    platform: InMemoryPlatform
    mode: AdPlatformMode = field(default=AdPlatformMode.LOCAL, init=False)

    async def get_bid(self, shop_id: str, keyword_id: str) -> float:
        return await self.platform.get_bid(shop_id, keyword_id)

    async def set_bid(self, shop_id: str, keyword_id: str, value: float,
                      idempotency_key: str) -> None:
        await self.platform.set_bid(shop_id, keyword_id, value, idempotency_key)

    # --- 给测试/演示用的透传，方便断言"平台只被调用一次" ---
    @property
    def calls(self) -> list:
        return self.platform.calls

    @property
    def bids(self) -> dict:
        return self.platform.bids
