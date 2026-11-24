import asyncio
import math
import os
import random
import uuid
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Header, HTTPException, Response

from .chaos import Chaos, chaos
from .limiter import TokenBucket

app = FastAPI(title="mock-amazon")

BUCKETS = {
    "orders": TokenBucket(rate=1.0, burst=5),
    "order_detail": TokenBucket(rate=2.0, burst=10),
    "inventory": TokenBucket(rate=2.0, burst=10),
}
ORDERS: dict = {}
INVENTORY: dict = {}
CALLBACK_URL = os.getenv("CALLBACK_URL", "http://hub:8000/webhook/amazon")
# 测试时可以替换成进程内的投递函数，不走真实网络
PUSH_HOOK = None


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def limit(name: str, response: Response):
    ok, info = BUCKETS[name].take()
    if not ok:
        raise HTTPException(
            status_code=429,
            detail="QuotaExceeded",
            # 向上取整到两位小数，且至少 0.01：四舍五入会少算，导致重试时额度还没攒够
            headers={"Retry-After": f"{max(0.01, math.ceil(info * 100) / 100):.2f}"},
        )
    response.headers["x-ratelimit-remaining"] = f"{info:.2f}"


@app.get("/orders")
async def list_orders(response: Response, updated_after: str | None = None,
                      x_shop_id: str = Header(...)):
    limit("orders", response)
    items = [o for o in ORDERS.values() if o["shop_id"] == x_shop_id]
    if updated_after:
        items = [o for o in items if o["last_update_time"] > updated_after]
    return {"orders": items}


@app.get("/orders/{order_id}")
async def get_order(order_id: str, response: Response, x_shop_id: str = Header(...)):
    limit("order_detail", response)
    order = ORDERS.get(order_id)
    if not order or order["shop_id"] != x_shop_id:
        raise HTTPException(404, "NotFound")
    return order


@app.get("/inventory/{sku}")
async def get_inventory(sku: str, response: Response):
    limit("inventory", response)
    return {"sku": sku, "quantity": INVENTORY.get(sku, 0)}


@app.post("/_admin/chaos")
async def set_chaos(cfg: Chaos):
    for k, v in cfg.model_dump().items():
        setattr(chaos, k, v)
    return chaos


@app.post("/_admin/orders")
async def create_or_update_order(order_id: str, shop_id: str, status: str = "Pending",
                                 ts: str | None = None):
    new_ts = ts or now_iso()
    cur = ORDERS.get(order_id)
    # 真实平台以最新时间为准：旧时间的修改不会覆盖新状态，但事件照样会推送
    if cur is None or cur["last_update_time"] < new_ts:
        ORDERS[order_id] = {
            "order_id": order_id, "shop_id": shop_id, "status": status,
            "last_update_time": new_ts,
        }
    event = {
        "event_id": str(uuid.uuid4()), "shop_id": shop_id, "type": "ORDER_CHANGE",
        "order_id": order_id, "status": status,
        "event_time": new_ts,
    }
    await push(event)
    return event


async def push(event: dict):
    if random.random() < chaos.drop_rate:
        return
    if chaos.delay_ms:
        await asyncio.sleep(chaos.delay_ms / 1000)
    copies = [event]
    if random.random() < chaos.duplicate_rate:
        copies.append(event)
    if PUSH_HOOK is not None:
        for e in copies:
            await PUSH_HOOK(e)
        return
    async with httpx.AsyncClient(timeout=5) as client:
        for e in copies:
            try:
                await client.post(CALLBACK_URL, json=e)
            except httpx.HTTPError:
                pass
