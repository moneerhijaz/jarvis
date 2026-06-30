"""Voice service tests that don't require the optional models to be installed."""
import pytest

from jarvis.config import load_settings
from jarvis.voice.service import VoiceService, VoiceUnavailable


def test_voice_health_structure(tmp_path):
    s = load_settings(overrides={"vault": {"path": str(tmp_path / "brain")}})
    h = VoiceService(s).health()
    assert "stt" in h and "tts" in h
    assert h["stt"]["ready"] is False  # lazy: not loaded yet
    assert h["tts"]["ready"] is False


def test_speak_requires_configured_model(tmp_path):
    # tts_model_path is blank -> deterministic failure regardless of piper install
    s = load_settings(overrides={
        "vault": {"path": str(tmp_path / "brain")},
        "voice": {"tts_model_path": ""},
    })
    with pytest.raises(VoiceUnavailable):
        VoiceService(s).speak("hello")


def test_ack_router(tmp_path):
    from jarvis.app import Application
    from jarvis.model.fake import FakeModelClient, final_step

    base = {
        "app": {"data_dir": str(tmp_path / "d"), "logs_dir": str(tmp_path / "l"), "artifacts_dir": str(tmp_path / "a")},
        "vault": {"path": str(tmp_path / "brain"), "embed": {"mode": "none"}},
    }
    # No dedicated fast model -> instant keyword heuristic (no model call).
    app = Application(load_settings(overrides=base), model=FakeModelClient())
    # a greeting / simple question gets no filler
    assert app.ack_for("how are you?")["ack"] is False
    # a task-like request gets a short spoken filler
    d = app.ack_for("list every folder in my projects directory")
    assert d["ack"] is True and d["phrase"]
    app.close()


def test_voice_health_endpoint(tmp_path):
    from fastapi.testclient import TestClient
    from jarvis.app import Application
    from jarvis.model.fake import FakeModelClient

    s = load_settings(overrides={
        "app": {"data_dir": str(tmp_path / "data"), "logs_dir": str(tmp_path / "logs"),
                "artifacts_dir": str(tmp_path / "art")},
        "vault": {"path": str(tmp_path / "brain"), "embed": {"mode": "none"}},
    })
    from jarvis.api.server import create_app

    app = Application(s, model=FakeModelClient())
    client = TestClient(create_app(app))
    r = client.get("/api/voice/health")
    assert r.status_code == 200
    assert "stt" in r.json()
    app.close()
