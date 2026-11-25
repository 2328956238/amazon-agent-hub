"""Checkpointer：把编排状态持久化到独立的 SQLite 文件（与业务库分离）。

加固要点（第三阶段收尾）：
1. 路径解析：绝对路径原样使用；**相对路径锚定到项目根**，不受启动 CWD 影响；
2. 父目录：不存在则安全创建（只创建目录，不碰任何已有文件）；
3. 权限：Unix 下把 checkpoint 文件收紧到 0600（只允许当前运行用户读写）；
   Windows 不适用 POSIX 位，跳过并记录；
4. 失败模式：路径不可用 / 目录无法创建 / 文件不可写 / 打不开 → 抛
   `CheckpointUnavailable`，**绝不静默退回 InMemorySaver**；
5. 资源：`finally` 里关闭连接，异常路径也会关。

依赖：langgraph-checkpoint-sqlite（内部用 aiosqlite）。
"""

from __future__ import annotations

import logging
import os
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from .. import config

logger = logging.getLogger("hub.checkpoint")


class CheckpointUnavailable(RuntimeError):
    """checkpoint 存储不可用。API 层据此返回 503，并只给通用错误文案。"""

    code = "checkpoint_unavailable"


def resolve_checkpoint_path(path: str | None = None) -> Path:
    """解析 checkpoint 文件路径。

    - 绝对路径：原样使用（`.resolve()` 归一化，不跟随不存在部分做严格检查）
    - 相对路径：相对**项目根**解析，而不是进程 CWD
    - 目标已存在且是目录：直接报错
    """
    raw = Path(path or config.GRAPH_CHECKPOINT_DB).expanduser()
    if not raw.is_absolute():
        raw = config.PROJECT_ROOT / raw
    resolved = raw.resolve()
    if resolved.exists() and resolved.is_dir():
        raise CheckpointUnavailable(f"checkpoint 路径是目录而不是文件: {resolved}")
    return resolved


def _ensure_parent_dir(db_path: Path) -> None:
    """确保父目录存在；父路径是文件或无权限时明确报错。"""
    parent = db_path.parent
    if parent.exists():
        if not parent.is_dir():
            raise CheckpointUnavailable(f"checkpoint 父路径不是目录: {parent}")
        return
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CheckpointUnavailable(
            f"无法创建 checkpoint 目录 {parent}: {type(exc).__name__}: {exc}") from exc
    logger.info("已创建 checkpoint 目录: %s", parent)


def _check_writable(db_path: Path, parent: Path) -> None:
    target = db_path if db_path.exists() else parent
    if not os.access(target, os.W_OK):
        raise CheckpointUnavailable(
            f"checkpoint 位置不可写: {target}（请检查目录权限或换 GRAPH_CHECKPOINT_DB）")


def _restrict_permissions(db_path: Path) -> None:
    """Unix：0600（仅属主可读写）。Windows：跳过（用 ACL，不套 POSIX 位）。"""
    if os.name != "posix":
        logger.debug("非 POSIX 平台，跳过 0600 权限设置: %s", db_path)
        return
    try:
        os.chmod(db_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        raise CheckpointUnavailable(
            f"无法设置 checkpoint 文件权限(0600): {db_path}: {exc}") from exc
    mode = stat.S_IMODE(db_path.stat().st_mode)
    logger.info("checkpoint 文件权限已收紧: %s mode=0o%o", db_path, mode)


def checkpoint_info(path: str | None = None) -> dict[str, object]:
    """给启动日志/健康检查用的只读信息（不返回文件内容）。"""
    p = resolve_checkpoint_path(path)
    exists = p.exists()
    return {
        "path": str(p),
        "exists": exists,
        "size": p.stat().st_size if exists else 0,
        "mode": oct(stat.S_IMODE(p.stat().st_mode)) if exists and os.name == "posix" else None,
    }


@asynccontextmanager
async def open_sqlite_checkpointer(path: str | None = None) -> AsyncIterator[object]:
    """打开（必要时创建）SQLite checkpointer，用完后关闭连接。

    用法：
        async with open_sqlite_checkpointer() as saver:
            graph = build_graph(deps, checkpointer=saver)
            await graph.ainvoke(state, config)
    """
    try:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    except ImportError as exc:
        raise CheckpointUnavailable(
            "缺少 langgraph-checkpoint-sqlite 或 aiosqlite，无法持久化工作流状态") from exc

    db_path = resolve_checkpoint_path(path)
    _ensure_parent_dir(db_path)
    _check_writable(db_path, db_path.parent)

    try:
        conn = await aiosqlite.connect(str(db_path))
    except Exception as exc:  # noqa: BLE001 - 统一转成明确的不可用错误
        raise CheckpointUnavailable(
            f"无法打开 checkpoint 数据库 {db_path}: {type(exc).__name__}: {exc}") from exc

    try:
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        _restrict_permissions(db_path)
        logger.info("checkpoint 已就绪: %s", db_path)
        yield saver
    finally:
        await conn.close()
        logger.debug("checkpoint 连接已关闭: %s", db_path)
