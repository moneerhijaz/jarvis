# JARVIS UI — holographic voice console

A React + Vite front end for the JARVIS backend: a reactive holographic core you
talk to (voice-first), with a live agent activity timeline and a text-chat toggle.

## What it does

- **Voice (default):** tap the core (or the mic button) → speak → release. Your
  speech is transcribed locally (faster-whisper), the agent runs, the spoken
  answer comes back via local TTS (Piper). The core reacts through states:
  idle → listening → thinking → speaking.
- **Live timeline:** the model's thoughts, each tool call + result, warnings, and
  the final answer stream in over SSE as the agent works.
- **Text mode (non-default):** the `⌨ text` toggle (top-right) switches to a
  typed command box.
- **Status:** shows whether LM Studio is reachable and which brain model is live.

## Prerequisites

1. Backend running with deps installed (from `alpha/prod`):
   ```powershell
   .\.venv\Scripts\Activate.ps1
   pip install -e ".[dev,voice]"      # 'voice' adds faster-whisper + piper-tts
   ```
2. **Local voice models:**
   - STT: faster-whisper downloads its model automatically on first use
     (`stt_model: base` in `config/local.yaml`; change to `small`/`medium` for
     more accuracy).
   - TTS: download a Piper voice (e.g. `en_US-amy-medium`) from
     https://huggingface.co/rhasspy/piper-voices — you need the `.onnx` and its
     `.onnx.json`. Put them somewhere (e.g. `C:/Users/hijaz/JarvisBrain/.voices/`)
     and set in `config/local.yaml`:
     ```yaml
     voice:
       tts_model_path: C:/Users/hijaz/JarvisBrain/.voices/en_US-amy-medium.onnx
     ```
   Without a TTS voice, everything still works — JARVIS just won't speak (the
   answer is shown as text). Without the `voice` extra installed, the mic path
   returns a clear "voice unavailable" message and you can use text mode.
3. Node 20+.

## Run (development)

Two terminals from `alpha/prod`:

```powershell
# Terminal 1 — backend (http://127.0.0.1:8765)
.\.venv\Scripts\Activate.ps1
python -m jarvis.main serve

# Terminal 2 — UI (http://localhost:5173, proxies /api to the backend)
cd ui
npm install
npm run dev
```

Open http://localhost:5173. Mic + audio require a "secure context" — `localhost`
counts, so this works without HTTPS.

## Run (production, one origin)

```powershell
cd ui
npm install
npm run build          # outputs ui/dist
cd ..
python -m jarvis.main serve   # backend auto-serves ui/dist at http://127.0.0.1:8765
```

## Notes

- Voice is fully local: audio never leaves your machine. (Contrast with browser
  speech APIs, which send audio to the cloud — deliberately not used here.)
- The core also works mouse/keyboard-only via text mode if you prefer silence.
- Wake-word ("Jarvis…") and the heavier control-room screens (run history, vault
  browser, audit/rollback, settings) are the planned next additions.
