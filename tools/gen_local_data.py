"""生成本地模拟业务数据（可重复执行，结果确定）。

用途：给 amazon-agent-hub 提供可演示的 Amazon 运营数据（商品/订单/库存/价格/广告）。
声明：**全部为合成数据（synthetic），不是真实卖家数据，也不是官方 Sandbox 返回**。
一致性约束（脚本强制）：
- 每个订单的 asin/sku 必须存在于同店商品
- 订单金额 = 数量 × 商品单价（两位小数）
- 库存 on_hand >= 0，reserved <= on_hand，available = on_hand - reserved
- 价格 / 广告里的 asin 必须存在
- 时间用 UTC ISO8601，金额统一 USD

运行：.\.venv\Scripts\python.exe tools\gen_local_data.py
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "amazon"
BASE_DAY = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)

PRODUCTS = [
    # shop_id, asin, sku, title, brand, category, price, description
    ("S1", "B0TEST0001", "SKU-CASE-001", "Silicone Phone Case for iPhone 15 (Matte Black)",
     "AcmeGear", "Electronics > Phone Accessories", 19.99,
     "Slim shockproof case, compatible with iPhone 15, matte finish."),
    ("S1", "B0TEST0002", "SKU-CHARGER-65W", "65W USB-C GaN Fast Charger 2-Port",
     "AcmeGear", "Electronics > Chargers", 39.99,
     "Dual-port GaN charger, PD 3.0, foldable plug, travel friendly."),
    ("S1", "B0TEST0003", "SKU-PAD-15W", "15W Wireless Charging Pad with USB-C",
     "AcmeGear", "Electronics > Chargers", 24.50,
     "Qi-certified 15W wireless pad, includes 1m USB-C cable."),
    ("S2", "B0TEST0101", "SKU-HUB-7IN1", "USB-C Hub 7-in-1 with HDMI 4K",
     "OtherBrand", "Electronics > Hubs", 29.99,
     "7-in-1 hub: HDMI, USB 3.0 x3, SD, microSD, PD 100W."),
    ("S2", "B0TEST0102", "SKU-CABLE-2M", "USB-C to USB-C Cable 2m 100W",
     "OtherBrand", "Electronics > Cables", 9.99,
     "Braided 100W charging cable, 480Mbps data."),
]

# 正常场景的 7 天销量（用于库存覆盖天数计算）
SALES_PLAN = {
    ("S1", "SKU-CASE-001"): 35,      # 日均 5
    ("S1", "SKU-CHARGER-65W"): 28,   # 日均 4
    ("S1", "SKU-PAD-15W"): 21,       # 日均 3
    ("S2", "SKU-HUB-7IN1"): 14,
    ("S2", "SKU-CABLE-2M"): 7,
}

INVENTORY = [
    # shop_id, sku, asin, warehouse, on_hand, reserved, inbound
    ("S1", "SKU-CASE-001", "B0TEST0001", "FBA-US-EAST", 120, 8, 60),
    ("S1", "SKU-CHARGER-65W", "B0TEST0002", "FBA-US-EAST", 18, 5, 0),
    ("S1", "SKU-PAD-15W", "B0TEST0003", "FBA-US-EAST", 40, 4, 0),
    ("S2", "SKU-HUB-7IN1", "B0TEST0101", "FBA-US-WEST", 60, 3, 0),
    ("S2", "SKU-CABLE-2M", "B0TEST0102", "FBA-US-WEST", 200, 10, 0),
]

PRICING = [
    # shop_id, asin, currency, price, competitor_price, buybox, updated_at
    ("S1", "B0TEST0001", "USD", 19.99, 21.50, True, "2026-01-10T09:00:00+00:00"),
    ("S1", "B0TEST0002", "USD", 39.99, 37.99, False, "2026-01-10T09:00:00+00:00"),
    ("S1", "B0TEST0003", "USD", 24.50, 24.99, True, "2026-01-10T09:00:00+00:00"),
    ("S2", "B0TEST0101", "USD", 29.99, 31.00, True, "2026-01-10T09:00:00+00:00"),
    ("S2", "B0TEST0102", "USD", 9.99, 10.50, True, "2026-01-10T09:00:00+00:00"),
]

ADVERTISING = [
    # shop_id, asin, campaign, keyword, impressions, clicks, orders, spend, sales, period_days
    ("S1", "B0TEST0001", "SP-CASE-AUTO", "phone case iphone 15", 4200, 210, 9, 84.00, 179.91, 7),
    ("S1", "B0TEST0002", "SP-CHARGER-EXACT", "65w gan charger", 2600, 130, 6, 62.40, 239.94, 7),
    ("S1", "B0TEST0003", "SP-PAD-BROAD", "wireless charger pad", 3100, 90, 2, 55.00, 49.00, 7),
    ("S2", "B0TEST0101", "SP-HUB-AUTO", "usb c hub hdmi", 1500, 60, 3, 21.00, 89.97, 7),
    ("S2", "B0TEST0102", "SP-CABLE-EXACT", "usb c cable 2m", 900, 40, 2, 9.00, 19.98, 7),
]


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def build_products() -> list[dict]:
    return [{
        "shop_id": shop, "asin": asin, "sku": sku, "title": title, "brand": brand,
        "category": category, "price": price, "currency": "USD",
        "description": desc, "status": "active",
        "source": "synthetic",
    } for shop, asin, sku, title, brand, category, price, desc in PRODUCTS]


def build_orders() -> list[dict]:
    """按 SALES_PLAN 生成 7 天订单；金额严格等于 数量 × 单价。"""
    price = {(p[0], p[2]): p[6] for p in PRODUCTS}
    product_of_sku = {(p[0], p[2]): p[1] for p in PRODUCTS}
    orders: list[dict] = []
    seq = 0
    statuses = ["Shipped", "Shipped", "Shipped", "Pending", "Canceled"]
    for (shop, sku), total_qty in SALES_PLAN.items():
        asin = product_of_sku[(shop, sku)]
        remaining = total_qty
        day_offset = 6
        while remaining > 0:
            qty = min(3, remaining)
            remaining -= qty
            seq += 1
            status = statuses[seq % len(statuses)]
            purchased = BASE_DAY - timedelta(days=day_offset, minutes=(seq * 37) % 600)
            orders.append({
                "shop_id": shop,
                "order_id": f"{shop}-{1000 + seq:04d}-{seq:02d}",
                "asin": asin,
                "sku": sku,
                "quantity": qty,
                "currency": "USD",
                "item_price": price[(shop, sku)],
                "order_total": round(qty * price[(shop, sku)], 2),
                "status": status,
                "purchase_date": _iso(purchased),
                "fulfillment": "FBA",
                "channel": "Amazon",
                "source": "synthetic",
            })
            day_offset = max(0, day_offset - 1)
    orders.sort(key=lambda o: o["purchase_date"], reverse=True)
    return orders


def build_inventory() -> list[dict]:
    return [{
        "shop_id": shop, "sku": sku, "asin": asin, "warehouse": wh,
        "on_hand": on_hand, "reserved": reserved, "inbound": inbound,
        "available": on_hand - reserved, "currency": "USD",
        "updated_at": "2026-01-10T09:00:00+00:00", "source": "synthetic",
    } for shop, sku, asin, wh, on_hand, reserved, inbound in INVENTORY]


def build_pricing() -> list[dict]:
    return [{
        "shop_id": shop, "asin": asin, "currency": currency, "price": price,
        "competitor_price": comp, "is_buybox": buybox, "updated_at": updated,
        "source": "synthetic",
    } for shop, asin, currency, price, comp, buybox, updated in PRICING]


def build_advertising() -> list[dict]:
    return [{
        "shop_id": shop, "asin": asin, "campaign": campaign, "keyword": keyword,
        "impressions": impressions, "clicks": clicks, "orders": orders,
        "spend": spend, "sales": sales, "period_days": days,
        "currency": "USD", "end_date": "2026-01-10", "source": "synthetic",
    } for shop, asin, campaign, keyword, impressions, clicks, orders, spend, sales, days
        in ADVERTISING]


def build_scenarios() -> dict[str, dict]:
    return {
        "normal": {
            "name": "normal", "description": "正常销售场景：库存充足、广告表现健康",
            "source": "synthetic", "overrides": {},
            "expected": {"low_stock_skus": [], "inventory_risk_levels": {}},
        },
        "low_inventory": {
            "name": "low_inventory", "description": "库存不足场景：充电器可售仅 4 件，日均卖 4 件",
            "source": "synthetic",
            "overrides": {"inventory": [
                {"shop_id": "S1", "sku": "SKU-CHARGER-65W", "asin": "B0TEST0002",
                 "warehouse": "FBA-US-EAST", "on_hand": 6, "reserved": 2, "inbound": 0,
                 "available": 4, "currency": "USD",
                 "updated_at": "2026-01-10T09:00:00+00:00", "source": "synthetic"},
            ]},
            "expected": {"low_stock_skus": ["SKU-CHARGER-65W"],
                         "inventory_risk_levels": {"SKU-CHARGER-65W": "critical"}},
        },
        "order_spike": {
            "name": "order_spike", "description": "订单突增场景：最近 2 天充电器新增 24 单，库存覆盖迅速下降",
            "source": "synthetic",
            "overrides": {"orders_append": [
                {"shop_id": "S1", "asin": "B0TEST0002", "sku": "SKU-CHARGER-65W",
                 "quantity": 2, "count": 12, "status": "Pending", "days_ago": 0},
            ]},
            "expected": {"order_spike_skus": ["SKU-CHARGER-65W"]},
        },
        "advertising_drop": {
            "name": "advertising_drop", "description": "广告花费上升但销售额下降：ACOS 明显恶化",
            "source": "synthetic",
            "overrides": {"advertising": [
                {"shop_id": "S1", "asin": "B0TEST0002", "campaign": "SP-CHARGER-EXACT",
                 "keyword": "65w gan charger", "impressions": 2600, "clicks": 130, "orders": 2,
                 "spend": 95.00, "sales": 79.98, "period_days": 7,
                 "currency": "USD", "end_date": "2026-01-10", "source": "synthetic"},
            ]},
            "expected": {"advertising_levels": {"B0TEST0002": "critical"}},
        },
        "price_anomaly": {
            "name": "price_anomaly", "description": "价格异常场景：无线充价格 2.50，远低于竞品 24.99",
            "source": "synthetic",
            "overrides": {"pricing": [
                {"shop_id": "S1", "asin": "B0TEST0003", "currency": "USD", "price": 2.50,
                 "competitor_price": 24.99, "is_buybox": True,
                 "updated_at": "2026-01-10T09:00:00+00:00", "source": "synthetic"},
            ]},
            "expected": {"price_anomaly_asins": ["B0TEST0003"]},
        },
    }


def write(name: str, payload) -> None:
    path = DATA / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")


def validate(products, orders, inventory, pricing, advertising) -> None:
    asins = {(p["shop_id"], p["asin"]) for p in products}
    skus = {(p["shop_id"], p["sku"]) for p in products}
    price = {(p["shop_id"], p["sku"]): p["price"] for p in products}
    for o in orders:
        assert (o["shop_id"], o["asin"]) in asins, f"订单 asin 不在商品表: {o}"
        assert (o["shop_id"], o["sku"]) in skus, f"订单 sku 不在商品表: {o}"
        assert abs(o["order_total"] - round(o["quantity"] * price[(o["shop_id"], o["sku"])], 2)) < 1e-9
    for i in inventory:
        assert (i["shop_id"], i["sku"]) in skus
        assert (i["shop_id"], i["asin"]) in asins
        assert i["on_hand"] >= 0 and 0 <= i["reserved"] <= i["on_hand"]
        assert i["available"] == i["on_hand"] - i["reserved"]
    for p in pricing:
        assert (p["shop_id"], p["asin"]) in asins
    for a in advertising:
        assert (a["shop_id"], a["asin"]) in asins
    print(f"validated: products={len(products)} orders={len(orders)} "
          f"inventory={len(inventory)} pricing={len(pricing)} advertising={len(advertising)}")


def main() -> None:
    products = build_products()
    orders = build_orders()
    inventory = build_inventory()
    pricing = build_pricing()
    advertising = build_advertising()
    validate(products, orders, inventory, pricing, advertising)

    write("products.json", {"meta": _meta("Catalog Items API / Listing 元数据形状（简化）"), "items": products})
    write("orders.json", {"meta": _meta("Orders API 扁平化订单视图"), "items": orders})
    write("inventory.json", {"meta": _meta("FBA Inventory 简化视图"), "items": inventory})
    write("pricing.json", {"meta": _meta("Product Pricing 简化视图"), "items": pricing})
    write("advertising.json", {"meta": _meta("广告报表聚合视图（按关键词/周期）"), "items": advertising})

    for name, payload in build_scenarios().items():
        write(f"scenarios/{name}.json", payload)


def _meta(mapping: str) -> dict:
    return {
        "source": "synthetic",
        "data_kind": "本地模拟业务数据",
        "declaration": "非真实卖家数据、非官方 Sandbox 返回、非实时库存；仅用于演示与测试",
        "field_mapping": mapping,
        "currency": "USD",
        "timezone": "UTC",
        "reference_date": "2026-01-10",
    }


if __name__ == "__main__":
    main()
