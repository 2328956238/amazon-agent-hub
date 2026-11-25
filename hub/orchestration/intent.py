"""意图识别：规则优先，LLM 兜底（LLM 可注入，不强制调用真实模型）。

为什么规则优先：
1. 跨境电商运营的说法高度固定（"ACOS 高"、"库存不够"、"价格要降"），规则命中率高；
2. 规则可解释、可测试、零成本、零延迟；
3. 只有规则判不出来时才走 LLM，把模型用在它真正擅长的地方。
"""

from __future__ import annotations

from typing import Awaitable, Callable

from .state import Intent

# 关键词按「业务含义」分组；同一个词不要出现在多组里，避免命中数打平后靠顺序决定。
# 注意：AD_PERFORMANCE 刻意不收 "花费" / "ACOS" —— 那两个属于广告诊断（ADVERTISING），
# 避免 "广告花费太高，出价要降" 被误判成纯分析。
RULES: dict[Intent, tuple[str, ...]] = {
    Intent.OVERVIEW: (
        "综合", "整体", "概况", "运营分析", "巡检", "全店", "盘一下", "体检",
    ),
    Intent.AD_PERFORMANCE: (
        "广告表现", "广告效果", "广告分析", "广告数据", "表现分析", "投放表现",
    ),
    Intent.ORDER: (
        "订单", "最近单", "发货", "待处理", "取消单", "unshipped", "订单状态",
    ),
    Intent.ADVERTISING: (
        "广告", "出价", "竞价", "acos", "roas", "花费", "点击", "曝光", "转化率",
        "关键词", "campaign", "ctr", "cvr", "烧钱", "预算",
    ),
    Intent.INVENTORY: (
        "库存", "补货", "缺货", "断货", "安全库存", "fba", "在途", "备货", "周转", "可售",
    ),
    Intent.PRICING: (
        "定价", "售价", "调价", "涨价", "降价", "价格", "利润", "毛利", "底价",
    ),
    Intent.PRODUCT: (
        "商品", "listing", "标题", "五点", "主图", "描述", "上架", "下架", "类目", "变体",
    ),
}

# 注入点：真实模型只需实现同样的签名（返回意图名或 None）
IntentLLM = Callable[[str], Awaitable[str | None]]


def classify_by_rules(message: str) -> tuple[Intent, int]:
    """返回（意图，命中关键词数）。命中数为 0 表示规则判不出来。"""
    text = (message or "").lower()
    if not text.strip():
        return Intent.UNKNOWN, 0

    best_intent, best_score = Intent.UNKNOWN, 0
    for intent, words in RULES.items():
        score = sum(1 for w in words if w in text)
        # 命中数相同就保持 RULES 的先后顺序（广告优先），避免结果不稳定
        if score > best_score:
            best_intent, best_score = intent, score
    return best_intent, best_score


async def classify(message: str, intent_llm: IntentLLM | None = None) -> tuple[Intent, str]:
    """识别意图，返回（意图，来源）。来源取值：rule / llm / fallback / unknown。"""
    intent, score = classify_by_rules(message)
    if score > 0:
        return intent, "rule"

    if intent_llm is not None:
        try:
            raw = await intent_llm(message)
        except Exception:
            # 模型侧异常不能把编排打挂，降级成 UNKNOWN 交给 fail 节点
            return Intent.UNKNOWN, "fallback"
        if raw:
            try:
                return Intent(str(raw).strip().lower()), "llm"
            except ValueError:
                return Intent.UNKNOWN, "fallback"

    return Intent.UNKNOWN, "fallback"
