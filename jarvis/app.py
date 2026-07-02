"""Application container: builds and wires every backend component from Settings.

This is the composition root. Tests construct it with an injected model client
(the FakeModelClient) for deterministic, offline runs; production uses the LM
Studio client.
"""
from __future__ import annotations

import logging
import os
import time
import uuid

from jarvis.pipeline.runner import PipelineRunner
from jarvis.pipeline.types import RunRequest, RunResult
from jarvis.config import Settings, load_settings
from jarvis.events import EventSink
from jarvis.memory.retrieval import Retriever
from jarvis.memory.vault import Vault
from jarvis.model.client import ModelClient
from jarvis.model.lmstudio import LMStudioClient
from jarvis.security.policy import Policy
from jarvis.security.rollback import RollbackManager
from jarvis.data.store import Store
from jarvis.tools import ToolExecutor, build_default_registry
from jarvis.voice import VoiceService

logger = logging.getLogger("jarvis.app")

import random

_ACK_PHRASES = ["One moment.", "Let me check that.", "On it.", "Give me a second.", "Sure, working on it."]

_TASK_HINTS = (
    "list", "open", "run", "find", "search", "organize", "delete", "move", "create",
    "write", "install", "summar", "size", "how big", "rename", "build", "fix", "edit",
    "download", "remember", "schedule",
)


def _looks_like_task(message: str) -> bool:
    m = message.lower()
    return len(m) > 80 or any(h in m for h in _TASK_HINTS)


class Application:
    def __init__(self, settings: Settings | None = None, model: ModelClient | None = None) -> None:
        self.settings = settings or load_settings()
        self.settings.ensure_dirs()

        self.store = Store(self.settings.db_path)
        self.events = EventSink(persist=lambda ev: self.store.add_event(ev.model_dump()))

        self.model: ModelClient = model or self._build_model()
        # Optional small/fast router model (e.g. for the ack decision). Falls back
        # to the brain if the 'fast' role isn't enabled.
        fast_role = self.settings.role("fast")
        self.fast_model: ModelClient | None = (
            self._build_role_client("fast") if (model is None and fast_role.enabled) else None
        )

        self.registry = build_default_registry()
        self.executor = ToolExecutor(self.registry, self.events, self.store)

        self.vault = Vault(self.settings.vault_path)
        # Memory v2: build the embedder ONCE and share it between the index (writes vectors on
        # sync) and the retriever (semantic search). May be None if embeddings aren't configured.
        self._embedder = self._build_embedder()
        from jarvis.memory.index import VaultIndex
        self.vault.attach_index(VaultIndex(self.store, self.vault, embed_fn=self._embedder,
                                           embed_model=self.settings.vault.embed.model))
        self.retriever = Retriever(
            self.vault,
            embed_fn=self._embedder,
            graph_hops=self.settings.vault.retrieval.graph_hops,
            max_snippets=self.settings.vault.retrieval.max_snippets,
        )
        self.rollback = RollbackManager(
            self.settings.artifacts_path / "snapshots",
            persist=lambda r: self.store.add_rollback({
                "token": r.token, "kind": r.kind, "target": r.target,
                "snapshot_path": r.snapshot_path, "manifest": r.manifest, "created_at": r.created_at,
            }),
            store=self.store,
        )
        self.rollback.load()  # restore durability across restarts
        self.policy = Policy(
            delete_threshold=self.settings.autonomy.delete_threshold,
            tripwires_enabled=self.settings.autonomy.tripwires_enabled,
        )
        self.voice = VoiceService(self.settings)
        # The dataflow pipeline is the one and only engine.
        self.loop = PipelineRunner(
            self.settings, self.model, self.registry, self.executor, self.events,
            store=self.store, vault=self.vault, retriever=self.retriever,
            rollback=self.rollback, policy=self.policy,
        )

        self._results: dict[str, RunResult] = {}
        self._tasks: dict[str, object] = {}
        self._clients: dict[str, ModelClient] = {}   # per-model client cache for auto-routing
        from jarvis.confirm import ConfirmBroker
        self.confirm = ConfirmBroker()               # risky-action approval gate
        self.loop.confirm = self.confirm
        self.loop.resolve_model = self.client_for

    # -- builders ---------------------------------------------------------- #
    def _brain_endpoint(self) -> tuple[str, str, int]:
        """Effective (base_url, api_key, timeout) for the brain — may point at another LAN PC."""
        prov = self.settings.provider_for("brain")
        m = self.settings.models
        return (m.brain_base_url or prov.base_url, m.brain_api_key or prov.api_key, prov.timeout_s)

    def _build_role_client(self, role_name: str) -> ModelClient:
        base, key, timeout = self._brain_endpoint()
        role = self.settings.role(role_name)
        return LMStudioClient(base_url=base, api_key=key, model=role.model, timeout_s=timeout)

    def _build_model(self) -> ModelClient:
        return self._build_role_client("brain")

    def _build_embedder(self):
        mode = self.settings.vault.embed.mode
        if mode == "lmstudio":
            # Dedicated client on the configured embedding model (NOT the brain),
            # so indexing uses the right model and never depends on the chat model.
            prov = self.settings.provider_for("brain")
            embed_model = self.settings.vault.embed.model
            client = LMStudioClient(base_url=prov.base_url, api_key=prov.api_key,
                                    model=embed_model, timeout_s=prov.timeout_s)
            return lambda texts: client.embed(texts)
        if mode == "in_process":
            # Local-first: use the HF cache only — no network, no "unauthenticated requests to the
            # HF Hub" warning. Export HF_HUB_OFFLINE=0 for the one-time model download on a fresh
            # machine; after that it stays fully offline.
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
            try:  # optional dependency
                from sentence_transformers import SentenceTransformer  # type: ignore

                st_model = SentenceTransformer(self.settings.vault.embed.model)
                return lambda texts: [list(map(float, v)) for v in st_model.encode(texts)]
            except Exception:
                logger.warning(
                    "in_process embedder unavailable (install the 'embed' extra); "
                    "falling back to keyword+graph retrieval", exc_info=True
                )
                return None
        return None  # keyword + graph only

    def client_for(self, model_id: str | None) -> ModelClient:
        """Return a (cached) client for a specific model id, or the brain client."""
        if not model_id:
            return self.model
        c = self._clients.get(model_id)
        if c is None:
            base, key, timeout = self._brain_endpoint()
            c = LMStudioClient(base_url=base, api_key=key, model=model_id, timeout_s=timeout)
            self._clients[model_id] = c
        return c

    def set_endpoints(self, brain_url: str | None = None, vision_url: str | None = None) -> dict:
        """Point the brain and/or vision models at any LAN address (persisted). The backend
        and control tools stay on THIS machine."""
        from jarvis.config import persist_setting
        m = self.settings.models
        if brain_url is not None:
            m.brain_base_url = brain_url.strip()
            persist_setting("models", "brain_base_url", '"%s"' % m.brain_base_url)
            self._clients.clear()                       # rebuild clients against the new host
            self.model = self._build_model()
            self.loop.model = self.model
        if vision_url is not None:
            m.vision_base_url = vision_url.strip()
            persist_setting("models", "vision_base_url", '"%s"' % m.vision_base_url)
        return {"brain_url": m.brain_base_url, "vision_url": m.vision_base_url}

    def route_model(self, message: str) -> str | None:
        """When auto mode is on, choose a model id for this request by difficulty."""
        if not self.settings.models.auto:
            return None
        hard = _looks_like_task(message) or len(message) > 120
        chosen = self.settings.models.auto_complex if hard else self.settings.models.auto_simple
        return chosen or None

    def set_brain_model(self, model_id: str) -> None:
        """Switch the brain at runtime. 'auto' enables difficulty-based routing;
        any other id pins that model and disables auto."""
        from jarvis.config import RoleCfg

        if model_id == "auto":
            self.settings.models.auto = True
            logger.info("model routing set to AUTO (simple=%s, complex=%s)",
                        self.settings.models.auto_simple, self.settings.models.auto_complex)
            return
        self.settings.models.auto = False
        role = self.settings.models.roles.get("brain")
        if role is None:
            role = RoleCfg()
            self.settings.models.roles["brain"] = role
        role.model = model_id
        self.model = self._build_model()
        self.loop.model = self.model
        logger.info("brain model pinned to %s", model_id)

    # -- ack router: decide if a quick spoken filler is warranted ---------- #
    def ack_for(self, message: str) -> dict:
        """Should JARVIS say a brief filler before working? Returns
        {ack: bool, phrase: str|None}.

        If a dedicated 'fast' model is configured, it decides + phrases. Otherwise
        we use an INSTANT keyword heuristic rather than calling the brain -- on a
        single-model runtime an LLM ack just queues behind / delays the real answer.
        """
        if self.fast_model is None:
            if _looks_like_task(message):
                return {"ack": True, "phrase": random.choice(_ACK_PHRASES)}
            return {"ack": False, "phrase": None}

        from jarvis.model.client import Message as M

        system = (
            "You gate a voice assistant's optional one-line filler. The user just "
            "spoke to it. If the request can be answered instantly (a greeting, "
            "chit-chat, or a simple question), reply with exactly: NONE. If it needs "
            "real work (files, folders, search, commands, computation, or multiple "
            "steps), reply with a SHORT spoken filler of at most 6 words, e.g. "
            "'One moment.' or 'Let me check that.'. Reply with ONLY the filler or NONE."
        )
        try:
            resp = self.fast_model.chat([M(role="system", content=system), M(role="user", content=message)],
                                        temperature=0.0, max_tokens=16)
            text = (resp.content or "").strip()
        except Exception:
            logger.warning("ack router failed; using heuristic", exc_info=True)
            if _looks_like_task(message):
                return {"ack": True, "phrase": random.choice(_ACK_PHRASES)}
            return {"ack": False, "phrase": None}
        up = text.upper()
        if not text or up.startswith("NONE"):
            return {"ack": False, "phrase": None}
        return {"ack": True, "phrase": text.strip().strip('"').splitlines()[0][:60]}

    # -- vault lifecycle --------------------------------------------------- #
    def ensure_vault(self) -> None:
        if not self.vault.exists():
            self.vault.scaffold()
        if self.vault.index is not None:  # populate/refresh the derived catalog
            try:
                self.vault.index.reindex_all()
                # Memory v2 one-time housekeeping: drop stale auto-logs out of recall, then embed
                # any notes still missing a vector (no-op when embeddings are disabled).
                archived = self.store.archive_operational_logs()
                if archived:
                    logger.info("archived %d stale operational logs from recall", archived)
                self.vault.index.backfill_vectors()
            except Exception:
                logger.warning("initial vault reindex failed", exc_info=True)

    def consolidate_memory(self) -> dict:
        """Run the memory librarian on demand (UI button): merge duplicates, promote recurring
        facts to long-term, decay/archive stale ones, and group related memories into concept
        notes with descriptions. Deterministic parts always run; grouping needs the embedder."""
        from jarvis.memory.librarian import Librarian
        lib = Librarian(self.store, self.vault, embed_fn=self._embedder)
        rep = lib.consolidate()
        groups = lib.group_and_describe(self._group_label_fn) if self._embedder is not None else 0
        return {"merged": rep.merged, "promoted": rep.promoted, "archived": rep.archived,
                "groups": groups}

    def _group_label_fn(self, texts: list[str]):
        """LLM label for a memory cluster — supplies only kind + description (the title is derived
        deterministically by the librarian, so a weak model can't mislabel a group)."""
        from jarvis.model.client import Message
        from jarvis.pipeline.runner import _extract_json
        sysp = ("You organize a personal memory vault. Given related snippets, reply with ONLY a "
                'JSON object of REAL values, e.g. {"kind":"topic","description":"Notes about the '
                'user\'s coffee habits."} — do NOT copy the example. "kind" is "entity" if they are '
                'all about ONE specific person/project/tool, otherwise "topic". /no_think')
        try:
            resp = self.model.chat(
                [Message(role="system", content=sysp),
                 Message(role="user", content="Snippets:\n" + "\n".join(f"- {t}" for t in texts))],
                temperature=0.0, max_tokens=160)
            return _extract_json(resp.content or "")
        except Exception:
            logger.warning("group label call failed; librarian will use fallback", exc_info=True)
            return None

    # -- run management ---------------------------------------------------- #
    def new_run(self, message: str, thread_id: str | None = None, working_directory: str | None = None,
                project: str | None = None) -> RunRequest:
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        # One ongoing conversation by default: every turn shares the "main" thread,
        # so JARVIS remembers the past without any chat-history UI.
        thread_id = thread_id or "main"
        self.store.create_thread(thread_id, "Conversation")
        self.store.create_run({
            "id": run_id, "thread_id": thread_id, "status": "queued", "user_request": message,
            "working_directory": working_directory, "project": project,
            "model_provider": getattr(self.model, "provider", ""), "model_name": None,
            "started_at": None, "ended_at": None, "summary": None, "error_code": None, "error_message": None,
        })
        return RunRequest(run_id=run_id, thread_id=thread_id, message=message,
                          working_directory=working_directory, project=project,
                          model_id=self.route_model(message))

    async def run_sync(self, req: RunRequest) -> RunResult:
        """Run to completion and return the result (used by tests + CLI)."""
        res = await self.loop.run(req)
        self._results[req.run_id] = res
        return res

    def cancel(self, run_id: str) -> None:
        self.loop.request_cancel(run_id)
        self.confirm.cancel_all()   # release any pending approval so a cancelled run unblocks

    def panic(self) -> None:
        """Kill switch: abort EVERY active run, kill child processes, and release any
        pending confirmation. Wired to the global hotkey and POST /api/panic."""
        logger.warning("PANIC: aborting all runs and releasing control")
        for rid in list(self._tasks.keys()):
            try:
                self.loop.request_cancel(rid)
            except Exception:
                pass
        try:
            self.loop.kill_all_processes()
        except Exception:
            pass
        try:
            self.confirm.cancel_all()
        except Exception:
            pass

    def resolve_confirm(self, call_id: str, approved: bool) -> bool:
        return self.confirm.resolve(call_id, approved)

    def answer_question(self, question_id: str, text: str | None) -> bool:
        """Answer a Multiresponse question so a parked pipeline run can resume (text=None cancels).
        Only the pipeline engine supports this; other engines simply return False."""
        fn = getattr(self.loop, "answer_question", None)
        return bool(fn(question_id, text)) if fn else False

    def result(self, run_id: str) -> RunResult | None:
        return self._results.get(run_id)

    def close(self) -> None:
        self.store.close()
