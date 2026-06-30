"""FastAPI backend (PLANv3 §B.4 / PLANv2 §10).

Local-by-default HTTP + SSE. Routes cover health, models, threads/runs, run
event streaming, cancellation, tools, vault (memory), audit + rollback, and
settings. The web UI (later) consumes these.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("jarvis.api")

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from jarvis import __version__
from jarvis.app import Application
from jarvis.voice.service import VoiceUnavailable


class CreateRunBody(BaseModel):
    message: str
    thread_id: str | None = None
    working_directory: str | None = None
    project: str | None = None


class SearchBody(BaseModel):
    query: str
    project: str | None = None
    k: int = 8


class SpeakBody(BaseModel):
    text: str


class AckBody(BaseModel):
    message: str


class SelectModelBody(BaseModel):
    model: str


class ConfirmBody(BaseModel):
    call_id: str
    approved: bool


class AnswerBody(BaseModel):
    question_id: str
    text: str | None = None   # the user's answer to a Multiresponse question; null cancels


class PermBody(BaseModel):
    level: str   # every | risky | never


class VerifyBody(BaseModel):
    level: str   # auto | single | double


class AgreeBody(BaseModel):
    mode: str    # facts | model


class EndpointsBody(BaseModel):
    brain_url: str | None = None    # LAN base_url for the brain model ("" = local)
    vision_url: str | None = None   # LAN base_url for the vision model ("" = same as brain)


def _install_kill_hotkey(application: Application) -> None:
    """Register a global OS hotkey that triggers panic(). Needs the optional 'keyboard'
    package; if it's missing the UI Stop / pyautogui corner-failsafe still work."""
    au = application.settings.autonomy
    if not getattr(au, "enable_kill_switch", True) or not getattr(au, "kill_hotkey", ""):
        return

    def _go():
        try:
            import keyboard  # type: ignore
            keyboard.add_hotkey(au.kill_hotkey, application.panic)
            logger.info("kill-switch hotkey active: %s", au.kill_hotkey)
        except Exception as e:
            logger.info("kill-switch hotkey not active (install 'keyboard' to enable): %s", e)

    threading.Thread(target=_go, daemon=True).start()


def create_app(app: Application | None = None) -> FastAPI:
    application = app or Application()
    application.ensure_vault()
    # Warm STT/TTS in the background so the first voice request isn't a cold start.
    threading.Thread(target=application.voice.prewarm, daemon=True).start()
    _install_kill_hotkey(application)
    api = FastAPI(title="JARVIS", version=__version__)
    api.state.app = application

    # Local dev: the Vite UI runs on a different port and talks to this API.
    api.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Speech-Marks"],   # word timings for the subtitle tape
    )

    @api.middleware("http")
    async def _no_cache(request, call_next):
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"   # always serve the latest UI during dev
        return resp

    # ----- health / status ----- #
    def _have(mod: str) -> bool:
        import importlib.util
        try:
            return importlib.util.find_spec(mod) is not None
        except Exception:
            return False

    @api.get("/api/health")
    def health() -> dict[str, Any]:
        mh = application.model.health()
        return {
            "service": "ok",
            "version": __version__,
            "schema_version": application.store.schema_version,
            "model": mh.model_dump(),
            "vault": {"path": str(application.vault.root), "exists": application.vault.exists()},
            "voice": application.voice.health(),
            "autonomy": application.settings.autonomy.mode,
            "confirm_level": application.settings.autonomy.confirm_level,
            "verification": getattr(application.settings.autonomy, "verification", "auto"),
            "agreement": getattr(application.settings.autonomy, "agreement", "facts"),
            "capabilities": {                      # which agentic tools have their deps installed
                "vision_screen": _have("mss") and _have("PIL"),
                "input": _have("pyautogui"),
                "vision_targeting": _have("mss") and _have("PIL") and _have("pyautogui"),
                "web": True,                       # httpx is a core dependency
                "vision_model": application.settings.models.vision_model or "(auto-detect)",
            },
        }

    @api.get("/api/models")
    def models() -> dict[str, Any]:
        h = application.model.health().model_dump()
        h["auto"] = application.settings.models.auto
        h["current"] = "auto" if application.settings.models.auto else application.settings.role("brain").model
        h["vision"] = application.settings.models.vision_model   # "" = auto-detect
        h["grounding"] = application.settings.models.grounding_model   # "" = use vision model
        h["vision_mode"] = application.settings.models.vision_mode  # tool | context
        h["brain_url"] = application.settings.models.brain_base_url   # "" = local
        h["vision_url"] = application.settings.models.vision_base_url
        # Vision host may be a DIFFERENT machine — list ITS models (empty if unreachable,
        # never the brain's stale list). When no separate host, reuse the brain list.
        m = application.settings.models
        if m.vision_base_url:
            try:
                import httpx
                from jarvis.tools import vision as _vis
                base, key = _vis.lm_endpoint(application.settings)
                r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"}, timeout=3)
                h["vision_models"] = [d.get("id") for d in r.json().get("data", []) if d.get("id")]
                h["vision_reachable"] = True
            except Exception:
                h["vision_models"] = []
                h["vision_reachable"] = False
        else:
            h["vision_models"] = h.get("models", [])
            h["vision_reachable"] = bool(h.get("reachable"))
        return h

    @api.post("/api/models/select")
    def select_model(body: SelectModelBody) -> dict[str, Any]:
        application.set_brain_model(body.model)
        return {"ok": True, "brain": body.model}

    @api.post("/api/models/vision")
    def select_vision_model(body: SelectModelBody) -> dict[str, Any]:
        """Pin the model used for on-screen vision targeting. 'auto'/'' = auto-detect a VL model."""
        val = "" if body.model in ("auto", "") else body.model
        application.settings.models.vision_model = val
        try:
            from jarvis.config import persist_setting
            persist_setting("models", "vision_model", '""' if val == "" else val)
        except Exception:
            logger.warning("could not persist vision_model", exc_info=True)
        return {"ok": True, "vision_model": val or "auto"}

    @api.post("/api/models/grounding")
    def select_grounding_model(body: SelectModelBody) -> dict[str, Any]:
        """Pin a separate GUI-grounding model for click-locating. 'auto'/'' = use the vision model."""
        val = "" if body.model in ("auto", "") else body.model
        application.settings.models.grounding_model = val
        try:
            from jarvis.config import persist_setting
            persist_setting("models", "grounding_model", '""' if val == "" else val)
        except Exception:
            logger.warning("could not persist grounding_model", exc_info=True)
        return {"ok": True, "grounding_model": val or "auto"}

    @api.post("/api/models/vision_mode")
    def select_vision_mode(body: SelectModelBody) -> dict[str, Any]:
        """'context' = run the agent on the multimodal model (it sees screenshots directly);
        'tool' = strong text brain consults the vision model via tools."""
        mode = body.model if body.model in ("tool", "context") else "tool"
        application.settings.models.vision_mode = mode
        try:
            from jarvis.config import persist_setting
            persist_setting("models", "vision_mode", mode)
        except Exception:
            logger.warning("could not persist vision_mode", exc_info=True)
        return {"ok": True, "vision_mode": mode}

    # ----- runs ----- #
    @api.post("/api/runs")
    async def create_run(body: CreateRunBody) -> dict[str, Any]:
        req = application.new_run(body.message, body.thread_id, body.working_directory, body.project)
        # fire-and-forget; events stream over SSE
        task = asyncio.create_task(application.run_sync(req))
        application._tasks[req.run_id] = task
        return {"run_id": req.run_id, "thread_id": req.thread_id, "status": "queued",
                "events_url": f"/api/runs/{req.run_id}/events"}

    @api.get("/api/runs")
    def list_runs() -> dict[str, Any]:
        return {"runs": application.store.list_runs()}

    @api.get("/api/history")
    def history(thread_id: str = "main", limit: int = 200) -> dict[str, Any]:
        msgs = application.store.list_messages(thread_id, limit=limit)
        return {"messages": [{"role": m["role"], "content": m["content"]} for m in msgs]}

    @api.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        run = application.store.get_run(run_id)
        if not run:
            raise HTTPException(404, "run not found")
        run["events"] = application.store.get_events(run_id)
        return run

    @api.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str) -> dict[str, Any]:
        application.cancel(run_id)
        return {"run_id": run_id, "status": "cancel_requested"}

    @api.post("/api/runs/{run_id}/confirm")
    def confirm_action(run_id: str, body: ConfirmBody) -> dict[str, Any]:
        ok = application.resolve_confirm(body.call_id, body.approved)
        return {"ok": ok, "call_id": body.call_id, "approved": body.approved}

    @api.post("/api/runs/{run_id}/answer")
    def answer_question(run_id: str, body: AnswerBody) -> dict[str, Any]:
        """Answer a Multiresponse question so a parked pipeline run can resume."""
        ok = application.answer_question(body.question_id, body.text)
        return {"ok": ok, "question_id": body.question_id}

    @api.post("/api/panic")
    def panic() -> dict[str, Any]:
        """Kill switch: abort every active run + release control immediately."""
        application.panic()
        return {"ok": True}

    @api.get("/api/runs/{run_id}/events")
    async def stream_events(run_id: str, after_seq: int = 0):
        sub = await application.events.subscribe()

        async def gen():
            # replay persisted events first (reconnect-safe)
            for ev in application.store.get_events(run_id, after_seq):
                yield _sse(ev)
            # then live
            try:
                async for ev in sub.stream():
                    if ev.run_id != run_id:
                        continue
                    yield _sse(ev.model_dump())
                    if ev.type in ("run.completed", "run.failed", "run.cancelled"):
                        break
            finally:
                sub.close()

        return StreamingResponse(gen(), media_type="text/event-stream")

    # ----- tools ----- #
    @api.get("/api/tools")
    def list_tools() -> dict[str, Any]:
        return {"tools": [
            {"name": s.name, "namespace": s.namespace, "description": s.description,
             "risk": s.risk, "timeout_s": s.timeout_s, "always_on": s.always_on}
            for s in application.registry.all()
        ]}

    # ----- vault / memory ----- #
    @api.post("/api/vault/search")
    def vault_search(body: SearchBody) -> dict[str, Any]:
        snips = application.retriever.search(body.query, project=body.project, k=body.k)
        return {"results": [s.__dict__ for s in snips]}

    @api.get("/api/vault/profile")
    def vault_profile() -> dict[str, Any]:
        return {"profile": application.vault.read_profile(),
                "projects": application.vault.list_projects(),
                "skills": application.vault.list_skills()}

    @api.get("/api/vault/notes")
    def vault_notes() -> dict[str, Any]:
        notes = application.store.list_memory_notes()
        if not notes:  # catalog empty -> scan the Markdown directly
            notes = [{"vault_path": application.vault.rel(n.path), "title": n.title,
                      "type": n.meta.get("type", "note"), "summary": n.body[:160]}
                     for n in application.vault.all_notes()]
        return {"notes": notes}

    @api.get("/api/vault/note")
    def vault_note(path: str) -> dict[str, Any]:
        try:
            n = application.vault.read_note(path)
        except Exception:
            raise HTTPException(404, "note not found")
        return {"path": path, "title": n.title, "meta": n.meta, "body": n.body, "links": n.links}

    @api.get("/api/vault/graph")
    def vault_graph() -> dict[str, Any]:
        """Notes + resolved [[wikilink]] edges, for the brain graph visualization."""
        from jarvis.memory.graph import build_graph
        notes = application.vault.all_notes()
        g = build_graph(application.vault, notes)
        nodes = [{"id": application.vault.rel(n.path),
                  "title": n.title or application.vault.rel(n.path),
                  "type": (n.meta.get("type") if isinstance(n.meta, dict) else None) or "note"}
                 for n in notes]
        edges = [{"source": s, "target": t} for s, tgts in g.adj.items() for t in tgts]
        return {"nodes": nodes, "edges": edges}

    # ----- audit / rollback ----- #
    @api.get("/api/audit")
    def audit(run_id: str | None = None) -> dict[str, Any]:
        return {"audit": application.store.list_audit(run_id)}

    @api.post("/api/rollback/{token}")
    def rollback(token: str) -> dict[str, Any]:
        ok = application.rollback.rollback(token)
        return {"token": token, "restored": ok}

    # ----- ack router (should JARVIS say a quick filler?) ----- #
    @api.post("/api/ack")
    async def ack(body: AckBody) -> dict[str, Any]:
        t0 = time.perf_counter()
        res = await asyncio.to_thread(application.ack_for, body.message)
        logger.info("TIMING ack: %dms (ack=%s)", (time.perf_counter() - t0) * 1000, res.get("ack"))
        return res

    # ----- voice (local STT/TTS) ----- #
    @api.get("/api/voice/health")
    def voice_health() -> dict[str, Any]:
        return application.voice.health()

    @api.post("/api/voice/transcribe")
    async def voice_transcribe(audio: UploadFile = File(...)) -> dict[str, Any]:
        data = await audio.read()
        if not data:
            raise HTTPException(400, "empty audio (nothing was recorded)")
        suffix = "." + (audio.filename.rsplit(".", 1)[-1] if audio.filename and "." in audio.filename else "webm")
        t0 = time.perf_counter()
        try:
            res = await asyncio.to_thread(application.voice.transcribe, data, suffix)
        except VoiceUnavailable as e:
            raise HTTPException(503, str(e))
        except Exception as e:  # surface the real cause instead of a bare 500
            logger.exception("transcription failed")
            raise HTTPException(500, f"transcription failed: {type(e).__name__}: {e}")
        logger.info("TIMING stt transcribe: %dms (%d bytes)", (time.perf_counter() - t0) * 1000, len(data))
        return res

    @api.post("/api/voice/speak")
    async def voice_speak(body: SpeakBody):
        t0 = time.perf_counter()
        try:
            wav, marks = await asyncio.to_thread(application.voice.speak, body.text)
        except VoiceUnavailable as e:
            raise HTTPException(503, str(e))
        logger.info("TIMING tts speak: %dms (%d chars, %d bytes, %s marks)",
                    (time.perf_counter() - t0) * 1000, len(body.text), len(wav),
                    len(marks) if marks else 0)
        headers = {"X-Speech-Marks": json.dumps(marks)} if marks else None
        return Response(content=wav, media_type="audio/wav", headers=headers)

    @api.post("/api/settings/endpoints")
    def set_endpoints(body: EndpointsBody) -> dict[str, Any]:
        """Point the brain/vision models at any LAN host. Backend stays on this PC."""
        return {"ok": True, **application.set_endpoints(body.brain_url, body.vision_url)}

    # ----- settings ----- #
    @api.get("/api/settings")
    def get_settings() -> dict[str, Any]:
        return application.settings.model_dump()

    @api.post("/api/settings/permissions")
    def set_permissions(body: PermBody) -> dict[str, Any]:
        """Change the confirmation level live. 'never' = full control (admin runs without
        asking); 'risky' = confirm admin/destructive only; 'every' = confirm every action."""
        lvl = body.level if body.level in ("every", "risky", "never") else "risky"
        application.settings.autonomy.confirm_level = lvl
        if lvl == "never":
            application.confirm.cancel_all()   # release anything currently waiting
        try:   # persist so it survives a restart (preserves the config file's comments)
            from jarvis.config import persist_setting
            persist_setting("autonomy", "confirm_level", lvl)
        except Exception:
            logger.warning("could not persist confirm_level to config", exc_info=True)
        return {"ok": True, "confirm_level": lvl}

    @api.post("/api/settings/verification")
    def set_verification(body: VerifyBody) -> dict[str, Any]:
        """Change the verification level live. 'double' = brain+vision cross-check; 'single' =
        one pass; 'auto' = double when a 2nd model host exists. Applies to the next run."""
        lvl = body.level if body.level in ("auto", "single", "double") else "auto"
        application.settings.autonomy.verification = lvl
        try:
            from jarvis.config import persist_setting
            persist_setting("autonomy", "verification", lvl)
        except Exception:
            logger.warning("could not persist verification to config", exc_info=True)
        return {"ok": True, "verification": lvl}

    @api.post("/api/settings/agreement")
    def set_agreement(body: AgreeBody) -> dict[str, Any]:
        """How double-verify compares the two passes: 'facts' = deterministic containment;
        'model' = an extra model-judge call. Applies to the next run."""
        mode = body.mode if body.mode in ("facts", "model") else "facts"
        application.settings.autonomy.agreement = mode
        try:
            from jarvis.config import persist_setting
            persist_setting("autonomy", "agreement", mode)
        except Exception:
            logger.warning("could not persist agreement to config", exc_info=True)
        return {"ok": True, "agreement": mode}

    # ----- serve a UI at the same origin as /api -----
    # Prefer the built Vite app (ui/dist); fall back to the bundled no-build
    # single-file console (jarvis/web) so the UI works with zero Node toolchain.
    dist = Path(__file__).resolve().parents[2] / "ui" / "dist"
    web = Path(__file__).resolve().parents[1] / "web"   # jarvis/web (no-build fallback)
    serve_dir = dist if dist.exists() else (web if web.exists() else None)
    if serve_dir is not None:
        from fastapi.staticfiles import StaticFiles

        # Everything served lives under jarvis/web (incl. web/fonts and web/logos).
        api.mount("/", StaticFiles(directory=str(serve_dir), html=True), name="ui")

    return api


def _sse(ev: dict[str, Any]) -> str:
    return f"event: {ev['type']}\ndata: {json.dumps(ev, default=str)}\n\n"
