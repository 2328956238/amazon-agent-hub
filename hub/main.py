"""FastAPI 入口。

三套路由互不影响：
- `/webhook/amazon`：阶段一同步层（原有能力，未改动）
- `/api/v1/agent/*`：LangGraph 编排层（统一入口 / 运行查询 / 能力清单）
- `/api/v1/amazon/*`：本地合成业务数据只读接口（商品 / 订单 / 库存 / 广告 / 汇总）

任一子系统初始化失败只让对应路由返回 503，**不影响** webhook，也不会让应用起不来。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .adapters.local_data import LocalDataAdapter, LocalDataError
from .api.agent import build_orchestrator, router as agent_router
from .api.amazon import router as amazon_router
from .db import init_db
from .orchestration.checkpointer import open_sqlite_checkpointer
from .sync.events import router

logger = logging.getLogger("hub")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    # 本地合成数据（只读）
    app.state.local_data = None
    try:
        app.state.local_data = LocalDataAdapter()
    except LocalDataError:
        logger.exception("本地数据源初始化失败，/api/v1/amazon/* 将返回 503")

    app.state.orchestrator = None
    app.state.orchestrator_error = None
    app.state.checkpointer_cm = None
    cm = None
    try:
        cm = open_sqlite_checkpointer()
        saver = await cm.__aenter__()
        app.state.orchestrator = build_orchestrator(
            saver, data=getattr(app.state, "local_data", None))
        app.state.checkpointer_cm = cm
    except Exception as exc:  # noqa: BLE001 - 编排层不可用不能拖垮整个应用
        app.state.orchestrator_error = f"{type(exc).__name__}: {exc}"
        # 详细异常只进服务端日志，不进 HTTP 响应
        logger.exception("编排层初始化失败，Agent 接口将返回 503；webhook 不受影响")
        if cm is not None:
            try:
                await cm.__aexit__(type(exc), exc, exc.__traceback__)
            except Exception:  # pragma: no cover
                pass

    try:
        yield
    finally:
        if app.state.checkpointer_cm is not None:
            await app.state.checkpointer_cm.__aexit__(None, None, None)


app = FastAPI(title="hub", lifespan=lifespan)
app.include_router(router)          # /webhook/amazon（原有）
app.include_router(agent_router)    # /api/v1/agent/*（编排层）
app.include_router(amazon_router)   # /api/v1/amazon/*（本地合成数据只读）


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    detail = exc.detail
    error = detail if isinstance(detail, dict) and "code" in detail else {
        "code": "http_error", "message": str(detail)}
    return JSONResponse(status_code=exc.status_code, content={"ok": False, "error": error})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"ok": False, "error": {
        "code": "validation_error",
        "message": "请求参数不合法；shop_id 不允许由客户端指定，只能由认证上下文注入",
        "details": jsonable_encoder(exc.errors()),
    }})
