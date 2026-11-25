"""只读运营 Agent：商品 / 库存 / 订单 / 广告表现 / 综合运营。

共同约定：
1. 只用 LocalDataAdapter 读**本地合成数据**，不写任何平台、不需要审批；
2. 返回 payload 里必须带 `source: "synthetic"` 和声明，禁止说成真实 Amazon 数据；
3. 数据缺失抛 LocalDataError → 转成 FAILED + 可读错误，不静默返回空。

这些 Agent 替代了原先 product/inventory 的 UnimplementedAgent 占位；
Pricing 仍然保持未实现（写操作）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ...adapters.local_data import LocalDataAdapter, LocalDataError
from ..registry import AgentResult
from ..state import AgentState, Intent, RunStatus

ASIN_RE = re.compile(r"\b(B0[A-Z0-9]{8})\b")
SKU_RE = re.compile(r"\b(SKU-[A-Z0-9-]+)\b")
STATUS_WORDS = {"待处理": "Pending", "待发货": "Unshipped", "已发货": "Shipped", "取消": "Canceled"}


def extract_identifiers(message: str) -> tuple[str | None, str | None]:
    asin = ASIN_RE.search(message or "")
    sku = SKU_RE.search(message or "")
    return (asin.group(1) if asin else None, sku.group(1) if sku else None)


@dataclass
class OpsDeps:
    """只读 Agent 的统一依赖：数据适配器 + 数据源声明。"""

    data: LocalDataAdapter
    source_note: str = field(default=(
        "本地合成数据（synthetic），非真实卖家数据、非官方 Sandbox 返回、非实时库存"))

    @property
    def meta(self) -> dict[str, Any]:
        return {"source": "synthetic", "note": self.source_note}


class _ReadOnlyAgent:
    name = ""
    intents: tuple[Intent, ...] = ()
    implemented = True

    def __init__(self, deps: OpsDeps):
        self.deps = deps

    def _payload(self, **kw: Any) -> dict[str, Any]:
        base = {"agent": self.name, **self.deps.meta}
        base.update(kw)
        return base

    async def run(self, state: AgentState) -> AgentResult:  # pragma: no cover - 子类实现
        raise NotImplementedError


class ProductAgent(_ReadOnlyAgent):
    name = "product"
    intents = (Intent.PRODUCT,)

    async def run(self, state: AgentState) -> AgentResult:
        shop = state["shop_id"]
        asin, sku = extract_identifiers(state.get("message", ""))
        try:
            if asin or sku:
                product = self.deps.data.product(shop, asin=asin, sku=sku)
                prices = self.deps.data.prices(shop, asin=product["asin"])
                return AgentResult(status=RunStatus.COMPLETED.value, payload=self._payload(
                    query={"asin": asin, "sku": sku}, product=product,
                    pricing=prices[0] if prices else None, count=1))
            items = self.deps.data.products(shop)
            return AgentResult(status=RunStatus.COMPLETED.value, payload=self._payload(
                query={}, items=items, count=len(items)))
        except LocalDataError as exc:
            return AgentResult(status=RunStatus.FAILED.value,
                               errors=(f"{exc.code}: {exc.message}",))


class InventoryAgent(_ReadOnlyAgent):
    name = "inventory"
    intents = (Intent.INVENTORY,)
    RISK_WORDS = ("风险", "缺货", "断货", "补货", "周转", "可售", "安全库存")

    async def run(self, state: AgentState) -> AgentResult:
        shop = state["shop_id"]
        msg = state.get("message", "")
        asin, sku = extract_identifiers(msg)
        wants_risk = (not asin and not sku) or any(w in msg for w in self.RISK_WORDS)
        try:
            if not wants_risk:
                rows = self.deps.data.inventory(shop, asin=asin, sku=sku)
                if not rows:
                    return AgentResult(status=RunStatus.FAILED.value,
                                       errors=(f"not_found: 没有库存记录 asin={asin} sku={sku}",))
                return AgentResult(status=RunStatus.COMPLETED.value, payload=self._payload(
                    query={"asin": asin, "sku": sku},
                    items=rows, count=len(rows)))
            risks = self.deps.data.inventory_risk(shop)
            counts = {"critical": 0, "warn": 0, "ok": 0}
            for r in risks:
                counts[r.level] = counts.get(r.level, 0) + 1
            return AgentResult(status=RunStatus.COMPLETED.value, payload=self._payload(
                mode="risk", items=[r.to_dict() for r in risks],
                summary=counts, window_days=7,
                hint="critical = 可售不足 3 天；warn = 不足 7 天"))
        except LocalDataError as exc:
            return AgentResult(status=RunStatus.FAILED.value,
                               errors=(f"{exc.code}: {exc.message}",))


class OrdersAgent(_ReadOnlyAgent):
    name = "orders"
    intents = (Intent.ORDER,)
    DEFAULT_LIMIT = 10

    async def run(self, state: AgentState) -> AgentResult:
        shop = state["shop_id"]
        msg = state.get("message", "")
        asin, sku = extract_identifiers(msg)
        status = next((v for k, v in STATUS_WORDS.items() if k in msg), None)
        try:
            rows = self.deps.data.orders(shop, status=status, asin=asin, sku=sku,
                                         limit=self.DEFAULT_LIMIT)
            stats = self.deps.data.order_stats(shop, days=7)
            return AgentResult(status=RunStatus.COMPLETED.value, payload=self._payload(
                query={"asin": asin, "sku": sku, "status": status},
                items=rows, count=len(rows), stats=stats,
                note=f"最近 {self.DEFAULT_LIMIT} 条；统计窗口 7 天"))
        except LocalDataError as exc:
            return AgentResult(status=RunStatus.FAILED.value,
                               errors=(f"{exc.code}: {exc.message}",))


class AdPerformanceAgent(_ReadOnlyAgent):
    name = "ad_performance"
    intents = (Intent.AD_PERFORMANCE,)

    async def run(self, state: AgentState) -> AgentResult:
        shop = state["shop_id"]
        asin, _ = extract_identifiers(state.get("message", ""))
        try:
            perf = self.deps.data.ad_performance(shop, asin=asin)
            return AgentResult(status=RunStatus.COMPLETED.value, payload=self._payload(
                query={"asin": asin}, performance=perf,
                note="指标由 domain/advertising.py 计算（ACOS/ROAS/CTR/CVR + 阈值判断）"))
        except LocalDataError as exc:
            return AgentResult(status=RunStatus.FAILED.value,
                               errors=(f"{exc.code}: {exc.message}",))


class OverviewAgent(_ReadOnlyAgent):
    name = "overview"
    intents = (Intent.OVERVIEW,)

    async def run(self, state: AgentState) -> AgentResult:
        shop = state["shop_id"]
        data = self.deps.data
        try:
            risks = [r.to_dict() for r in data.inventory_risk(shop)]
            critical = [r for r in risks if r["level"] == "critical"]
            warn = [r for r in risks if r["level"] == "warn"]
            stats = data.order_stats(shop, days=7)
            perf = data.ad_performance(shop)
            anomalies = data.price_anomalies(shop)
            products = data.products(shop)

            high_acos = (perf["metrics"].get("acos") or 0) >= 0.5
            highlights: list[str] = []
            if critical:
                highlights.append(f"库存告急 {len(critical)} 个 SKU：" +
                                  ", ".join(r["sku"] for r in critical))
            if warn:
                highlights.append(f"库存预警 {len(warn)} 个 SKU")
            if high_acos:
                highlights.append(f"广告 ACOS {perf['metrics']['acos']:.1%} 偏高")
            if anomalies:
                highlights.append(f"价格异常 {len(anomalies)} 个 ASIN")
            if not highlights:
                highlights.append("各项指标正常")

            return AgentResult(status=RunStatus.COMPLETED.value, payload=self._payload(
                shop_id=shop,
                summary={"products": len(products), "orders_7d": stats["order_count"],
                         "revenue_7d": stats["revenue"], "inventory_critical": len(critical),
                         "inventory_warn": len(warn), "price_anomalies": len(anomalies),
                         "ad_level": perf["assessment"]["level"]},
                highlights=highlights, inventory_risk=risks, order_stats=stats,
                ad_performance=perf, price_anomalies=anomalies,
                note="综合运营巡检：只读汇总，任何写操作仍需审批链"))
        except LocalDataError as exc:
            return AgentResult(status=RunStatus.FAILED.value,
                               errors=(f"{exc.code}: {exc.message}",))
