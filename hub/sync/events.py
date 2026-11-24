from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session

router = APIRouter()

INSERT_EVENT = text("""
INSERT INTO platform_event (shop_id, event_id, type, payload)
VALUES (:shop_id, :event_id, :type, :payload)
ON CONFLICT (shop_id, event_id) DO NOTHING
""")

UPSERT_ORDER = text("""
INSERT INTO orders (shop_id, order_id, status, last_update_time, version)
VALUES (:shop_id, :order_id, :status, :ts, 1)
ON CONFLICT (shop_id, order_id) DO UPDATE
SET status = excluded.status,
    last_update_time = excluded.last_update_time,
    version = orders.version + 1
WHERE orders.last_update_time < excluded.last_update_time
""")


async def handle_event(session: AsyncSession, event: dict) -> str:
    """去重 + 防乱序，放在同一个事务里。返回 duplicate 或 ok。"""
    import json
    async with session.begin():
        res = await session.execute(INSERT_EVENT, {
            "shop_id": event["shop_id"], "event_id": event["event_id"],
            "type": event["type"], "payload": json.dumps(event),
        })
        if res.rowcount == 0:
            return "duplicate"
        if event["type"] == "ORDER_CHANGE":
            await session.execute(UPSERT_ORDER, {
                "shop_id": event["shop_id"], "order_id": event["order_id"],
                "status": event["status"], "ts": event["event_time"],
            })
    return "ok"


@router.post("/webhook/amazon")
async def receive(event: dict, session: AsyncSession = Depends(get_session)):
    return {"status": await handle_event(session, event)}
