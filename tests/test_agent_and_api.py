import pytest

from jarvis.app import Application
from jarvis.config import load_settings
from jarvis.model.fake import FakeModelClient, tool_step, final_step
from jarvis.security.redaction import redact


def _app(tmp_path, script):
    settings = load_settings(overrides={
        "app": {"data_dir": str(tmp_path / "data"), "logs_dir": str(tmp_path / "logs"),
                "artifacts_dir": str(tmp_path / "art")},
        "vault": {"path": str(tmp_path / "brain"), "embed": {"mode": "none"}},
    })
    app = Application(settings, model=FakeModelClient(script))
    app.ensure_vault()
    return app


@pytest.mark.asyncio
async def test_end_to_end_write_file_then_answer(tmp_path):
    script = [
        tool_step("fs.write", {"path": "note.txt", "content": "from jarvis"}, "c1"),
        final_step("Created note.txt"),
    ]
    app = _app(tmp_path, script)
    req = app.new_run("create a file", working_directory=str(tmp_path))
    res = await app.run_sync(req)
    assert res.status == "completed"
    assert res.final_answer == "Created note.txt"
    assert (tmp_path / "note.txt").read_text() == "from jarvis"
    # events were persisted and a run-summary note written to the vault
    assert app.store.get_events(req.run_id)
    assert list((app.vault.root / "daily").glob("run-*.md"))
    app.close()


@pytest.mark.asyncio
async def test_memory_remember_then_recall(tmp_path):
    # Run 1: remember a preference.
    app = _app(tmp_path, [tool_step("vault.capture", {"text": "I prefer dark mode."}, "c1"),
                          final_step("Noted.")])
    req = app.new_run("remember my preference")
    await app.run_sync(req)
    # Run 2 (new model script): recall via retriever.
    app.model = FakeModelClient([tool_step("vault.search", {"query": "mode preference"}, "c1"),
                                 final_step("You prefer dark mode.")])
    app.loop.model = app.model
    req2 = app.new_run("what mode do I prefer?")
    res2 = await app.run_sync(req2)
    assert res2.status == "completed"
    hits = app.retriever.search("mode preference")
    assert any("dark mode" in h.text for h in hits)
    app.close()


@pytest.mark.asyncio
async def test_step_budget_governor(tmp_path):
    # Model always asks to read the same file -> would loop forever without governors.
    app = _app(tmp_path, lambda msgs, tools: tool_step("fs.read", {"path": "nope.txt"}, "c1"))
    app.settings.limits.max_run_steps = 5
    req = app.new_run("loop forever")
    res = await app.run_sync(req)
    assert res.status in ("failed", "timed_out")
    assert res.steps <= 6
    app.close()


@pytest.mark.asyncio
async def test_no_progress_detector(tmp_path):
    app = _app(tmp_path, lambda msgs, tools: tool_step("fs.read", {"path": "same.txt"}, "c1"))
    app.settings.limits.max_run_steps = 50
    req = app.new_run("repeat the same call")
    res = await app.run_sync(req)
    assert res.status == "failed"
    assert "no_progress" in (res.error or "") or res.steps < 50
    app.close()


@pytest.mark.asyncio
async def test_conversation_continuity(tmp_path):
    # Two turns on the default "main" thread; the 2nd run must see the 1st exchange.
    app = _app(tmp_path, [final_step("Hi, I'm JARVIS."), final_step("Earlier you said hello.")])
    await app.run_sync(app.new_run("hello"))
    await app.run_sync(app.new_run("what did I say before?"))

    msgs = app.store.list_messages("main")
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[0]["content"] == "hello"

    # the second model call received the prior turns as context
    last_call_msgs = app.model.calls[-1]["messages"]
    blob = " ".join((m.content or "") for m in last_call_msgs)
    assert "Hi, I'm JARVIS." in blob   # prior assistant reply carried forward
    assert "hello" in blob             # prior user message carried forward
    app.close()


@pytest.mark.asyncio
async def test_max_tool_failures_governor(tmp_path):
    # Each step reads a DIFFERENT missing file, so the no-progress detector won't
    # fire (signatures differ) but failures accumulate -> max_tool_failures trips.
    counter = {"n": 0}

    def script(msgs, tools):
        counter["n"] += 1
        return tool_step("fs.read", {"path": f"missing_{counter['n']}.txt"}, f"c{counter['n']}")

    app = _app(tmp_path, script)
    app.settings.limits.max_tool_failures = 3
    app.settings.limits.max_run_steps = 50
    req = app.new_run("read missing files", working_directory=str(tmp_path))
    res = await app.run_sync(req)
    assert res.status == "failed"
    assert "max_tool_failures" in (res.error or "")
    assert res.steps <= 4
    app.close()


def test_redaction_blocks_secrets():
    s = redact("my api_key=SECRET12345 and token: ghp_abcdefghij1234567890")
    assert "SECRET12345" not in s
    assert "ghp_abcdefghij1234567890" not in s


def test_health_endpoint(tmp_path):
    from fastapi.testclient import TestClient
    from jarvis.api.server import create_app

    app = _app(tmp_path, [final_step("ok")])
    api = create_app(app)
    client = TestClient(api)
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "ok"
    assert body["vault"]["exists"] is True
    # tools listed
    assert any(t["name"] == "fs.write" for t in client.get("/api/tools").json()["tools"])
    app.close()
