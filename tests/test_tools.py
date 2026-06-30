import pytest

from jarvis.events import EventSink
from jarvis.model.client import ToolCall
from jarvis.security.policy import Policy
from jarvis.security.rollback import RollbackManager
from jarvis.tools import build_default_registry, ToolContext, ToolExecutor


def _ctx(tmp_path, **kw):
    rb = RollbackManager(tmp_path / "snap")
    return ToolContext(
        run_id="run_test", working_directory=str(tmp_path), settings=None,
        rollback=rb, policy=Policy(), **kw,
    )


@pytest.mark.asyncio
async def test_fs_write_read_roundtrip(tmp_path):
    reg = build_default_registry()
    ex = ToolExecutor(reg, EventSink())
    ctx = _ctx(tmp_path)
    r = await ex.execute(ToolCall(id="c1", name="fs.write", arguments={"path": "hello.txt", "content": "hi"}), ctx)
    assert r.ok and (tmp_path / "hello.txt").read_text() == "hi"
    r2 = await ex.execute(ToolCall(id="c2", name="fs.read", arguments={"path": "hello.txt"}), ctx)
    assert r2.ok and r2.data["content"] == "hi"


@pytest.mark.asyncio
async def test_fs_write_overwrite_snapshots_and_rolls_back(tmp_path):
    reg = build_default_registry()
    ex = ToolExecutor(reg, EventSink())
    ctx = _ctx(tmp_path)
    (tmp_path / "f.txt").write_text("original")
    r = await ex.execute(ToolCall(id="c1", name="fs.write", arguments={"path": "f.txt", "content": "new"}), ctx)
    assert r.rollback_token
    assert (tmp_path / "f.txt").read_text() == "new"
    assert ctx.rollback.rollback(r.rollback_token)
    assert (tmp_path / "f.txt").read_text() == "original"


@pytest.mark.asyncio
async def test_fs_delete_is_reversible(tmp_path):
    reg = build_default_registry()
    ex = ToolExecutor(reg, EventSink())
    ctx = _ctx(tmp_path)
    f = tmp_path / "trash.txt"
    f.write_text("bye")
    r = await ex.execute(ToolCall(id="c1", name="fs.delete", arguments={"path": "trash.txt"}), ctx)
    assert r.ok and not f.exists()
    # rollback restores it (fallback snapshot path on non-Windows)
    assert ctx.rollback.rollback(r.rollback_token)
    assert f.exists()


@pytest.mark.asyncio
async def test_bad_args_are_structured(tmp_path):
    reg = build_default_registry()
    ex = ToolExecutor(reg, EventSink())
    ctx = _ctx(tmp_path)
    r = await ex.execute(ToolCall(id="c1", name="fs.write", arguments={"path": "x"}), ctx)  # missing content
    assert not r.ok and r.error.category == "validation"


@pytest.mark.asyncio
async def test_shell_tripwire_refuses_catastrophic(tmp_path):
    reg = build_default_registry()
    ex = ToolExecutor(reg, EventSink())
    ctx = _ctx(tmp_path)
    r = await ex.execute(ToolCall(id="c1", name="shell.run", arguments={"command": "rm -rf /"}), ctx)
    assert not r.ok and r.error.category == "tripwire"


@pytest.mark.asyncio
async def test_shell_runs_command(tmp_path):
    reg = build_default_registry()
    ex = ToolExecutor(reg, EventSink())
    ctx = _ctx(tmp_path)
    r = await ex.execute(ToolCall(id="c1", name="shell.run", arguments={"command": "echo hello"}), ctx)
    assert r.ok and "hello" in (r.stdout or "")


def test_tool_subsetting_picks_relevant():
    reg = build_default_registry()
    chosen = reg.select_for("organize the files in my folder and delete duplicates")
    assert "tools.list" in chosen  # always-on
    assert any(n.startswith("fs.") for n in chosen)


def test_core_tools_always_available():
    # Regression: a "folder size" goal must still expose shell.run + fs.size even
    # though it contains no shell/run keywords.
    reg = build_default_registry()
    chosen = reg.select_for("get the size of the folder at C:/Users/me/projects/jarvis")
    for name in ("shell.run", "fs.size", "fs.list", "fs.read", "fs.write"):
        assert name in chosen, f"{name} should always be available"


@pytest.mark.asyncio
async def test_fs_size_recursive(tmp_path):
    reg = build_default_registry()
    ex = ToolExecutor(reg, EventSink())
    ctx = _ctx(tmp_path)
    (tmp_path / "a.txt").write_text("12345")          # 5 bytes
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("xx")      # 2 bytes
    r = await ex.execute(ToolCall(id="c1", name="fs.size", arguments={"path": "."}), ctx)
    assert r.ok
    assert r.data["bytes"] == 7
    assert r.data["files"] == 2
    assert r.data["human"]


def test_rollback_durable_across_restart(tmp_path):
    """A rollback token survives a process restart via the SQLite store."""
    from jarvis.data.store import Store
    from jarvis.security.rollback import RollbackManager

    store = Store(tmp_path / "db.sqlite")
    snap = tmp_path / "snap"

    def persist(r):
        store.add_rollback({
            "token": r.token, "kind": r.kind, "target": r.target,
            "snapshot_path": r.snapshot_path, "manifest": r.manifest, "created_at": r.created_at,
        })

    rb1 = RollbackManager(snap, persist=persist, store=store)
    f = tmp_path / "f.txt"
    f.write_text("orig")
    token = rb1.snapshot_file(f)
    f.write_text("new")

    # simulate a fresh process: a brand-new manager with only the DB to rely on
    rb2 = RollbackManager(snap, store=store)
    assert token not in rb2._records          # nothing in memory yet
    assert rb2.load() >= 1                     # hydrate from the store
    assert rb2.rollback(token) is True
    assert f.read_text() == "orig"
    assert store.get_rollback(token)["status"] == "restored"
