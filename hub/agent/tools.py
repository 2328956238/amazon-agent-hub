from dataclasses import dataclass
from datetime import date, timedelta

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import text


class ToolError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ToolContext:
    """由后端注入，模型无法修改。店铺隔离靠它，而不是靠模型自觉。"""
    shop_id: str
    today: str  # YYYY-MM-DD


class AdSummaryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")   # 模型传 shop_id 之类的多余参数会直接被拒
    days: int = Field(7, ge=1, le=30)


class InventoryArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str = Field(min_length=1, max_length=64)


async def get_ad_summary(session, ctx: ToolContext, args: AdSummaryArgs) -> dict:
    cutoff = (date.fromisoformat(ctx.today) - timedelta(days=args.days - 1)).isoformat()
    rows = (await session.execute(text("""
        SELECT keyword_id, keyword, SUM(spend), SUM(clicks), SUM(orders)
        FROM ad_report
        WHERE shop_id = :s AND day >= :c AND day <= :t
        GROUP BY keyword_id, keyword ORDER BY keyword_id
    """), {"s": ctx.shop_id, "c": cutoff, "t": ctx.today})).all()
    return {
        "days": args.days,
        "keywords": [
            {"keyword_id": r[0], "keyword": r[1], "spend": round(float(r[2]), 2),
             "clicks": int(r[3]), "orders": int(r[4])}
            for r in rows
        ],
    }


async def get_inventory(session, ctx: ToolContext, args: InventoryArgs) -> dict:
    row = (await session.execute(
        text("SELECT quantity FROM inventory WHERE shop_id = :s AND sku = :k"),
        {"s": ctx.shop_id, "k": args.sku})).first()
    return {"sku": args.sku, "quantity": int(row[0]) if row else 0}


# 白名单：名单外的工具一律拒绝
TOOLS = {
    "get_ad_summary": (AdSummaryArgs, get_ad_summary),
    "get_inventory": (InventoryArgs, get_inventory),
}


async def dispatch(session, ctx: ToolContext, name: str, raw_args: dict) -> dict:
    if name not in TOOLS:
        raise ToolError("tool_not_allowed")
    schema, fn = TOOLS[name]
    try:
        args = schema(**(raw_args or {}))
    except ValidationError:
        raise ToolError("bad_args")
    return await fn(session, ctx, args)
