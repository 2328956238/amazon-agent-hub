import os
from pathlib import Path

# 项目根目录：用于把相对路径锚定到仓库，而不是不确定的当前工作目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 默认 SQLite 方便直接跑；换成 PostgreSQL 只需改环境变量：
# DATABASE_URL=postgresql+asyncpg://hub:hub@localhost:5432/hub
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./hub.db")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
AMAZON_BASE_URL = os.getenv("AMAZON_BASE_URL", "http://localhost:9000")

# ---------------------------------------------------------------------------
# 编排层（LangGraph）
# ---------------------------------------------------------------------------
# 工作流 checkpoint 独立存一个 SQLite 文件，**与业务库分离**（不新增业务表）。
# 默认是绝对路径（挂在项目根目录下），避免"从别的目录启动就写一份新库"。
# 显式传入相对路径时，也按项目根解析，而不是 CWD。
GRAPH_CHECKPOINT_DB = os.getenv("GRAPH_CHECKPOINT_DB", str(PROJECT_ROOT / "hub_checkpoints.sqlite"))
# fake = 内置 FakeLLM（默认，离线）；real = 需自行注入真实模型客户端
LLM_MODE = os.getenv("LLM_MODE", "fake")
# 广告平台种子出价（仅 local adapter 用），格式 {"shop": {"keyword": bid}}
ADS_SEED_BIDS = os.getenv("ADS_SEED_BIDS", '{"S1": {"kw1": 1.0, "kw2": 0.8}}')
# 本地合成业务数据目录（商品/订单/库存/价格/广告 + scenarios + SP-API 示例）
AMAZON_DATA_DIR = os.getenv("AMAZON_DATA_DIR", str(PROJECT_ROOT / "data" / "amazon"))

# ---------------------------------------------------------------------------
# 运行环境
# ---------------------------------------------------------------------------
APP_ENV = os.getenv("APP_ENV", "dev").strip().lower()

# ---------------------------------------------------------------------------
# API 认证（shop_id 只能由后端映射出来，不能由客户端指定）
# ---------------------------------------------------------------------------
# 生产用：API_TOKENS='{"tok_abc": {"shop_id": "S1", "actor": "alice", "role": "operator"}}'
API_TOKENS = os.getenv("API_TOKENS", "")
DEFAULT_API_TOKEN = os.getenv("DEFAULT_API_TOKEN", "dev-token")
DEFAULT_SHOP_ID = os.getenv("DEFAULT_SHOP_ID", "S1")
DEFAULT_ACTOR = os.getenv("DEFAULT_ACTOR", "dev")
DEFAULT_ROLE = os.getenv("DEFAULT_ROLE", "admin")
