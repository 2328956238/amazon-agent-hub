import asyncio


class InMemoryPlatform:
    """内存版的广告平台，用来验证审批链。后续可替换成对接模拟亚马逊服务的 HTTP 实现。"""

    def __init__(self, bids: dict):
        self.bids = dict(bids)          # {(shop_id, keyword_id): 出价}
        self.calls = []                 # 每次 set_bid 调用都记录，便于断言“只调用一次”
        self.applied_keys = set()
        self.fail_next = 0              # 注入故障：接下来 N 次调用失败

    async def get_bid(self, shop_id: str, keyword_id: str) -> float:
        return self.bids[(shop_id, keyword_id)]

    async def set_bid(self, shop_id: str, keyword_id: str, value: float, idempotency_key: str):
        await asyncio.sleep(0)          # 让出事件循环，放大并发窗口
        self.calls.append((shop_id, keyword_id, value, idempotency_key))
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("platform error")
        if idempotency_key in self.applied_keys:   # 平台侧也按幂等键去重
            return
        self.applied_keys.add(idempotency_key)
        self.bids[(shop_id, keyword_id)] = value
