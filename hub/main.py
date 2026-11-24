from contextlib import asynccontextmanager

from fastapi import FastAPI

from .db import init_db
from .sync.events import router


@asynccontextmanager
async def lifespan(app):
    await init_db()
    yield


app = FastAPI(title="hub", lifespan=lifespan)
app.include_router(router)
