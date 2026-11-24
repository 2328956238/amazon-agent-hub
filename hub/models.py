from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint, JSON, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class PlatformEvent(Base):
    __tablename__ = "platform_event"
    __table_args__ = (UniqueConstraint("shop_id", "event_id", name="uq_event"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("shop_id", "order_id", name="uq_order"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[str] = mapped_column(String(64))
    order_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    last_update_time: Mapped[str] = mapped_column(String(40))
    version: Mapped[int] = mapped_column(Integer, default=1)


class SyncCursor(Base):
    """补拉水位线：只在补拉成功后推进，与订单表里的最大时间无关。"""
    __tablename__ = "sync_cursor"
    __table_args__ = (UniqueConstraint("shop_id", "name", name="uq_cursor"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(32))
    value: Mapped[str] = mapped_column(String(40))


# ---------- 阶段二：诊断 Agent 与审批链 ----------

class AdReport(Base):
    __tablename__ = "ad_report"
    __table_args__ = (UniqueConstraint("shop_id", "day", "keyword_id", name="uq_ad_report"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[str] = mapped_column(String(64))
    day: Mapped[str] = mapped_column(String(10))
    keyword_id: Mapped[str] = mapped_column(String(64))
    keyword: Mapped[str] = mapped_column(String(128))
    spend: Mapped[float] = mapped_column(Float)
    clicks: Mapped[int] = mapped_column(Integer)
    orders: Mapped[int] = mapped_column(Integer)


class Inventory(Base):
    __tablename__ = "inventory"
    __table_args__ = (UniqueConstraint("shop_id", "sku", name="uq_inventory"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[str] = mapped_column(String(64))
    sku: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[int] = mapped_column(Integer)


class DiagnosisRun(Base):
    __tablename__ = "diagnosis_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[str] = mapped_column(String(64))
    scenario: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32))
    steps: Mapped[int] = mapped_column(Integer, default=0)
    token_cost: Mapped[int] = mapped_column(Integer, default=0)


class ToolCallLog(Base):
    __tablename__ = "tool_call_log"
    __table_args__ = (UniqueConstraint("run_id", "call_id", name="uq_tool_call"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer)
    call_id: Mapped[str] = mapped_column(String(16))
    tool: Mapped[str] = mapped_column(String(64))
    args: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON)
    ok: Mapped[int] = mapped_column(Integer)


class Suggestion(Base):
    __tablename__ = "suggestion"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)   # 同时是执行的幂等键
    shop_id: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[int] = mapped_column(Integer)
    day: Mapped[str] = mapped_column(String(10))
    action: Mapped[str] = mapped_column(String(32))
    target: Mapped[str] = mapped_column(String(64))
    before_value: Mapped[float] = mapped_column(Float)              # 修改前快照，用于回滚
    after_value: Mapped[float] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list] = mapped_column(JSON)
    risk: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(24))
    updated_at: Mapped[str] = mapped_column(String(40))


class ApprovalLog(Base):
    __tablename__ = "approval_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    suggestion_id: Mapped[str] = mapped_column(String(36))
    step: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
