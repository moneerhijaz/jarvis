"""Cross-cutting checks that are NOT pipeline-stage behavior: secret redaction and the API
surface (health + tool listing).

The former agent-loop behavior tests (write-file-then-answer, memory recall, conversation
continuity, step-budget / no-progress / max-tool-failures governors) were removed with the
agent engine itself. Their pipeline equivalents live in test_pipeline_requests.py (end-to-end
request scenarios) and test_pipeline_engine.py (individual stages / replan / budgets)."""
from jarvis.app import Application
from jarvis.config import load_settings
from jarvis.model.fake import FakeModelClient, final_step
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
