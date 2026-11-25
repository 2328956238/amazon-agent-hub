"""任务二：Checkpoint 持久化加固测试（路径解析、目录创建、权限、失败模式）。"""

import os
import stat
from pathlib import Path

import pytest

from hub import config
from hub.orchestration import checkpointer as cp
from hub.orchestration.checkpointer import (
    CheckpointUnavailable,
    checkpoint_info,
    open_sqlite_checkpointer,
    resolve_checkpoint_path,
)


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------
def test_default_path_is_absolute_and_under_project_root():
    p = resolve_checkpoint_path()
    assert p.is_absolute()
    assert Path(config.PROJECT_ROOT) in p.parents
    assert p.name == "hub_checkpoints.sqlite"


def test_relative_path_is_resolved_against_project_root_not_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                       # 故意切到别的目录
    p = resolve_checkpoint_path("sub/dir/cp.sqlite")
    assert p == (Path(config.PROJECT_ROOT) / "sub/dir/cp.sqlite").resolve()
    assert not str(p).startswith(str(tmp_path))


def test_path_pointing_to_directory_is_rejected(tmp_path):
    with pytest.raises(CheckpointUnavailable) as exc:
        resolve_checkpoint_path(str(tmp_path))
    assert "目录" in str(exc.value)


# ---------------------------------------------------------------------------
# 目录创建与打开
# ---------------------------------------------------------------------------
async def test_creates_missing_parent_directories(tmp_path):
    db = tmp_path / "a" / "b" / "cp.sqlite"
    assert not db.parent.exists()
    async with open_sqlite_checkpointer(str(db)) as saver:
        assert saver is not None
    assert db.parent.is_dir()
    assert db.exists() and db.stat().st_size > 0


async def test_parent_is_a_file_raises_clear_error(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(CheckpointUnavailable) as exc:
        async with open_sqlite_checkpointer(str(blocker / "cp.sqlite")):
            pass
    assert "父路径不是目录" in str(exc.value) or "无法创建" in str(exc.value)


async def test_writable_check_raises_for_readonly_dir(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    if os.name == "posix":
        os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)      # 只读
    try:
        if os.access(ro, os.W_OK):                     # Windows/root 下可能仍可写，跳过
            pytest.skip("当前环境读目录仍可写，无法验证只读失败路径")
        with pytest.raises(CheckpointUnavailable) as exc:
            async with open_sqlite_checkpointer(str(ro / "cp.sqlite")):
                pass
        assert "不可写" in str(exc.value)
    finally:
        if os.name == "posix":
            os.chmod(ro, stat.S_IRWXU)


# ---------------------------------------------------------------------------
# 权限
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "posix", reason="POSIX 0600 权限仅 Unix 适用")
async def test_checkpoint_file_is_0600_on_posix(tmp_path):
    db = tmp_path / "cp.sqlite"
    async with open_sqlite_checkpointer(str(db)):
        pass
    mode = stat.S_IMODE(db.stat().st_mode)
    assert mode == 0o600, oct(mode)


async def test_checkpoint_info_reports_path_and_size(tmp_path):
    db = tmp_path / "cp.sqlite"
    async with open_sqlite_checkpointer(str(db)):
        pass
    info = checkpoint_info(str(db))
    assert info["exists"] is True
    assert info["size"] > 0
    assert str(info["path"]) == str(db)


# ---------------------------------------------------------------------------
# 资源释放 / 可重入
# ---------------------------------------------------------------------------
async def test_connection_is_closed_and_file_reusable(tmp_path):
    db = tmp_path / "cp.sqlite"
    async with open_sqlite_checkpointer(str(db)):
        pass
    # 连接关闭后能再次打开，说明没有悬挂句柄（Windows 上文件锁会直接暴露问题）
    for _ in range(2):
        async with open_sqlite_checkpointer(str(db)) as saver:
            assert saver is not None


async def test_init_failure_does_not_fall_back_to_memory(tmp_path, monkeypatch):
    """初始化失败必须抛错，不能悄悄换成 InMemorySaver。"""
    def boom(*args, **kwargs):
        raise CheckpointUnavailable("simulated init failure")

    monkeypatch.setattr(cp, "_ensure_parent_dir", boom)
    with pytest.raises(CheckpointUnavailable):
        async with open_sqlite_checkpointer(str(tmp_path / "cp.sqlite")):
            pass
