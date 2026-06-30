"""Local voice service: faster-whisper (STT) + Piper (TTS).

Both engines are optional and lazy-loaded: the backend imports and runs fine
without them (install the ``voice`` extra + download models to enable). Audio
never leaves the machine. Models load on first use so startup stays fast.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import time
import wave
from pathlib import Path
from typing import Any

logger = logging.getLogger("jarvis.voice")

_HF_ENV_DONE = False


def _setup_hf_env(settings) -> None:
    """Point the Hugging Face libraries (used by faster-whisper + kokoro) at a local
    cache and, when offline, stop them from ever hitting the network. Must run BEFORE
    the model libraries load. Idempotent."""
    global _HF_ENV_DONE
    if _HF_ENV_DONE:
        return
    v = settings.voice
    # Optionally keep all model weights inside the project so it's fully portable.
    if getattr(v, "models_dir", ""):
        root = Path(v.models_dir).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("HF_HOME", str(root))
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    if getattr(v, "offline", True):
        # Use the local cache only; never call huggingface.co. (Models must already be
        # downloaded once — see scripts/fetch-voice or set voice.offline: false briefly.)
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        logger.info("voice: HF offline mode ON (no network; using local model cache)")
    _HF_ENV_DONE = True

_URL_RE = re.compile(r"https?://\S+")


def clean_for_speech(text: str) -> str:
    """Turn an answer (which may contain markdown) into something natural to *say*.
    Removes code blocks, inline code, emphasis markers, headers, bullets, links,
    URLs and emoji so the voice doesn't read 'asterisk' or spell out symbols.
    """
    t = text or ""
    t = re.sub(r"```[\s\S]*?```", " (code) ", t)        # fenced code
    t = re.sub(r"`([^`]*)`", r"\1", t)                   # inline code
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)            # images
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)        # links -> text
    t = _URL_RE.sub(" link ", t)                          # bare URLs
    t = re.sub(r"^\s*#{1,6}\s*", "", t, flags=re.M)       # headers
    t = re.sub(r"^\s*[-*•]\s+", "", t, flags=re.M)        # bullets
    t = re.sub(r"[*_~#>|`]+", "", t)                      # leftover md symbols
    # drop emoji / non-speakable glyphs, keep sentence punctuation
    t = re.sub(r"[^\w\s.,!?;:'\"()\-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t or "Okay."


_PRON_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9']*")


def apply_pronunciations(text: str, settings=None) -> str:
    """Fix how the TTS engine says specific tokens. say_as_word forces a spoken form
    (JARVIS -> 'Jarvis'); spell_out renders an ALL-CAPS acronym as spaced letters
    (IDE -> 'I D E', which Kokoro/espeak reads as 'eye dee ee'). Both come from config
    so new terms need no code change. Word-for-word substitution keeps the tape's
    word-timing in step with what's actually spoken."""
    say = {"JARVIS": "Jarvis"}
    spell = {"IDE", "API", "URL", "GPU", "CPU", "UI", "UX", "LLM", "SSD", "USB",
             "HTTP", "HTTPS", "JSON", "HTML", "CSS", "SQL", "CLI", "SDK", "OS", "PC",
             "ID", "IP", "DNS", "VM", "AI", "TTS", "STT", "LAN", "VRAM", "GPT"}
    if settings is not None:
        v = settings.voice
        for k, val in (getattr(v, "say_as_word", {}) or {}).items():
            if val:
                say[str(k).upper()] = str(val)
        for k in (getattr(v, "spell_out", []) or []):
            spell.add(str(k).upper())

    def repl(m: "re.Match") -> str:
        tok = m.group(0)
        up = tok.upper()
        if up in say:
            return say[up]
        if tok.isupper() and len(tok) >= 2 and up in spell:
            return " ".join(tok)            # "IDE" -> "I D E"
        return tok

    return _PRON_TOKEN_RE.sub(repl, text)


def cap_for_speech(text: str, limit: int = 480) -> str:
    """Cap what gets SPOKEN so a long list/answer isn't read aloud forever (the
    full text is still shown on screen). Cuts at a sentence boundary near the limit."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind(". "), head.rfind("! "), head.rfind("? "))
    head = head[: cut + 1] if cut > limit * 0.5 else head
    return head.rstrip() + " And there's more on screen."


class VoiceUnavailable(RuntimeError):
    """Raised when a voice engine or its model is not available."""


class VoiceService:
    def __init__(self, settings) -> None:
        self.settings = settings
        self._stt = None          # faster_whisper.WhisperModel
        self._tts = None          # piper.PiperVoice
        self._stt_error: str | None = None
        self._tts_error: str | None = None

    # -- health ------------------------------------------------------------ #
    def health(self) -> dict[str, Any]:
        v = self.settings.voice
        return {
            "enabled": v.enabled,
            "stt": {
                "engine": "faster-whisper",
                "model": v.stt_model,
                "ready": self._stt is not None,
                "error": self._stt_error,
            },
            "tts": {
                "engine": v.tts_engine,
                "voice": v.kokoro_voice if v.tts_engine == "kokoro" else (v.tts_model_path or None),
                "ready": self._tts is not None,
                "error": self._tts_error,
            },
        }

    def prewarm(self) -> None:
        """Load STT + TTS models now (e.g. in a startup thread) so the first real
        request doesn't pay the import/model-load cold start."""
        try:
            self._ensure_stt()
        except Exception:
            logger.info("prewarm: STT not ready (deps/model missing) - will lazy-load")
        try:
            if (self.settings.voice.tts_engine or "piper").lower() == "kokoro":
                self._ensure_kokoro()
            else:
                self._ensure_tts()
        except Exception:
            logger.info("prewarm: TTS not ready - will lazy-load")

    # -- speech to text ---------------------------------------------------- #
    def _ensure_stt(self):
        if self._stt is not None:
            return self._stt
        _setup_hf_env(self.settings)
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except Exception as e:  # dependency missing
            self._stt_error = f"faster-whisper not installed: {e}"
            raise VoiceUnavailable(self._stt_error)
        v = self.settings.voice
        device = None if v.stt_device == "auto" else v.stt_device
        compute = None if v.stt_compute_type == "auto" else v.stt_compute_type
        kwargs: dict[str, Any] = {}
        if device:
            kwargs["device"] = device
        if compute:
            kwargs["compute_type"] = compute
        try:
            logger.info("loading faster-whisper model '%s' (device=%s, compute=%s) ...",
                        v.stt_model, device or "default", compute or "default")
            t0 = time.time()
            self._stt = WhisperModel(v.stt_model, **kwargs)
            logger.info("faster-whisper model loaded in %.1fs", time.time() - t0)
        except Exception as e:
            self._stt_error = f"failed to load whisper model '{v.stt_model}': {e}"
            raise VoiceUnavailable(self._stt_error)
        self._stt_error = None
        return self._stt

    def transcribe(self, audio_bytes: bytes, suffix: str = ".webm") -> dict[str, Any]:
        model = self._ensure_stt()
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        try:
            logger.info("transcribing %d bytes (%s) ...", len(audio_bytes), suffix)
            t0 = time.time()
            segments, info = model.transcribe(tmp_path, beam_size=1)
            text = "".join(seg.text for seg in segments).strip()
            logger.info("transcribed in %.1fs: %r", time.time() - t0, text[:120])
            return {"text": text, "language": getattr(info, "language", None)}
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                logger.warning("failed to remove temp audio %s", tmp_path, exc_info=True)

    # -- text to speech ---------------------------------------------------- #
    def _ensure_tts(self):
        if self._tts is not None:
            return self._tts
        v = self.settings.voice
        if not v.tts_model_path:
            self._tts_error = "voice.tts_model_path is not configured (download a Piper .onnx voice)"
            raise VoiceUnavailable(self._tts_error)
        try:
            from piper import PiperVoice  # type: ignore
        except Exception as e:
            self._tts_error = f"piper-tts not installed: {e}"
            raise VoiceUnavailable(self._tts_error)
        try:
            config_path = v.tts_config_path or (v.tts_model_path + ".json")
            self._tts = PiperVoice.load(v.tts_model_path, config_path=config_path)
        except Exception as e:
            self._tts_error = f"failed to load piper voice: {e}"
            raise VoiceUnavailable(self._tts_error)
        self._tts_error = None
        return self._tts

    def speak(self, text: str) -> tuple[bytes, list | None]:
        """Synthesize speech. Returns ``(wav_bytes, marks)`` where ``marks`` is an
        optional list of ``[word, start_seconds, end_seconds]`` word timings used by
        the UI to sync the subtitle tape to the audio. ``marks`` is ``None`` when the
        engine can't provide timing (e.g. Piper), in which case the UI estimates."""
        text = apply_pronunciations(cap_for_speech(clean_for_speech(text)), self.settings)
        engine = (self.settings.voice.tts_engine or "piper").lower()
        if engine == "kokoro":
            return self._kokoro_speak(text)
        return self._piper_speak(text), None

    def _piper_speak(self, text: str) -> bytes:
        voice = self._ensure_tts()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            with wave.open(tmp_path, "wb") as wav_file:
                # piper-tts API differs across versions; support both.
                if hasattr(voice, "synthesize_wav"):
                    voice.synthesize_wav(text, wav_file)
                else:
                    voice.synthesize(text, wav_file)
            return Path(tmp_path).read_bytes()
        finally:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                logger.warning("failed to remove temp wav %s", tmp_path, exc_info=True)

    # -- Kokoro (natural, light) ------------------------------------------ #
    def _ensure_kokoro(self):
        if self._tts is not None:
            return self._tts
        _setup_hf_env(self.settings)
        try:
            from kokoro import KPipeline  # type: ignore
        except Exception as e:
            self._tts_error = f"kokoro not installed: {e}"
            raise VoiceUnavailable(self._tts_error)
        v = self.settings.voice
        lang = v.kokoro_lang or (v.kokoro_voice[:1] if v.kokoro_voice[:1] in ("a", "b") else "a")
        try:
            logger.info("loading kokoro (lang=%s, voice=%s, device=%s) ...", lang, v.kokoro_voice, v.kokoro_device)
            try:
                self._tts = KPipeline(lang_code=lang, device=v.kokoro_device or None)
            except TypeError:  # older signature without device kwarg
                self._tts = KPipeline(lang_code=lang)
        except Exception as e:
            self._tts_error = f"failed to init kokoro: {e}"
            raise VoiceUnavailable(self._tts_error)
        self._tts_error = None
        return self._tts

    def _kokoro_speak(self, text: str) -> tuple[bytes, list | None]:
        import io

        import numpy as np  # bundled with kokoro/torch
        import soundfile as sf

        pipe = self._ensure_kokoro()
        v = self.settings.voice
        t0 = time.time()
        sr = 24000
        chunks: list = []
        marks: list = []           # [word, start_s, end_s] across the whole utterance
        chunk_offset = 0.0         # seconds of audio emitted before the current chunk
        speed = getattr(v, "kokoro_speed", 1.0) or 1.0
        voice = v.kokoro_voice or "bm_george"
        try:
            gen = pipe(text, voice=voice, speed=speed)
        except TypeError:           # older kokoro without a speed kwarg
            gen = pipe(text, voice=voice)
        for item in gen:
            audio = item[-1] if isinstance(item, (tuple, list)) else getattr(item, "audio", None)
            if audio is None:
                continue
            if hasattr(audio, "detach"):       # torch tensor
                audio = audio.detach().cpu().numpy()
            elif hasattr(audio, "numpy"):
                audio = audio.numpy()
            audio = np.asarray(audio, dtype="float32").reshape(-1)
            # Per-word timestamps (newer kokoro Result.tokens carry start_ts/end_ts,
            # relative to this chunk's audio). Best-effort: skip if unavailable.
            toks = getattr(item, "tokens", None)
            if toks:
                for tk in toks:
                    w = (getattr(tk, "text", "") or "").strip()
                    s = getattr(tk, "start_ts", None)
                    e = getattr(tk, "end_ts", None)
                    if not w:
                        continue
                    # Kokoro emits punctuation (. , ! ? etc.) as its own token; glue it
                    # onto the previous word so the tape doesn't show " word . " with gaps.
                    if marks and not any(ch.isalnum() for ch in w):
                        marks[-1][0] += w
                        if e is not None:
                            marks[-1][2] = chunk_offset + float(e)
                        continue
                    if s is not None and e is not None:
                        marks.append([w, chunk_offset + float(s), chunk_offset + float(e)])
            chunks.append(audio)
            chunk_offset += len(audio) / sr
        if not chunks:
            return b"", None
        full = np.concatenate(chunks).astype("float32")
        # Pad with a little silence so playback can't clip the first/last words.
        # Small pads: the warm AudioContext on the client handles most edge-clipping,
        # so keep these tight to avoid an obvious gap between streamed sentences.
        lead_s = 0.03
        lead = np.zeros(int(sr * lead_s), dtype="float32")   # 30 ms before
        trail = np.zeros(int(sr * 0.10), dtype="float32")    # 100 ms after
        full = np.concatenate([lead, full, trail])
        # Shift word marks to account for the lead pad so they line up with playback.
        if marks:
            marks = [[w, s + lead_s, e + lead_s] for (w, s, e) in marks]
        buf = io.BytesIO()
        sf.write(buf, full, sr, format="WAV", subtype="PCM_16")
        logger.info("kokoro synthesized %d samples (%d word marks) in %.1fs",
                    full.shape[0], len(marks), time.time() - t0)
        return buf.getvalue(), (marks or None)
