"""One-time online download of the voice models (STT + Kokoro TTS) into the local
Hugging Face cache, so the app can then run fully offline (voice.offline: true).

Run this ONCE while connected to the internet:

    .venv312\\Scripts\\python scripts\\fetch_voice.py

After it prints "voice models ready", JARVIS will never call huggingface.co again
(the backend forces HF offline mode when voice.offline is true).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.config import load_settings          # noqa: E402
from jarvis.voice.service import VoiceService     # noqa: E402


def main() -> int:
    settings = load_settings()
    settings.voice.offline = False   # allow network for this one-time fetch
    svc = VoiceService(settings)

    print("Downloading STT model (faster-whisper:%s) ..." % settings.voice.stt_model)
    try:
        svc._ensure_stt()
        print("  STT ready.")
    except Exception as e:
        print("  STT FAILED:", e)

    if (settings.voice.tts_engine or "").lower() == "kokoro":
        print("Downloading Kokoro TTS + voice '%s' ..." % settings.voice.kokoro_voice)
        try:
            svc._ensure_kokoro()
            # the voice file (e.g. af_heart) downloads lazily on first synth -> trigger it
            wav, _ = svc.speak("Voice models downloaded and ready.")
            print("  TTS ready (%d bytes synthesized)." % len(wav))
        except Exception as e:
            print("  TTS FAILED:", e)

    print("\nDone. Set voice.offline: true (default) and restart — no more network calls.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
