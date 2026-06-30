"""Event model and the EventSink (durable persistence + realtime fanout).

The agent core never writes to a socket directly. It emits typed events to the
EventSink, which (a) assigns a per-run sequence number, (b) persists the event
for replay, and (c) fans it out to subscribed realtime listeners (the SSE
endpoint). This is the single observability spine described in PLANv3 / PLANv2.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any, AsyncIterator, Callable

from pydantic import BaseModel, Field

logger = logging.getLogger("jarvis.events")


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Event(BaseModel):
    id: str
    run_id: str
    thread_id: str | None = None
    seq: int
    type: str
    timestamp: str = Field(default_factory=now_iso)
    payload: dict[str, Any] = Field(default_factory=dict)


# Event type constants (kept stable; they appear in logs, tests, and the UI).
RUN_CREATED = "run.created"
RUN_STARTED = "run.started"
RUN_STATUS = "run.status"
PLAN_CREATED = "plan.created"
PLAN_UPDATED = "plan.updated"
THOUGHT = "model.thought"
MODEL_STARTED = "model.started"
MODEL_COMPLETED = "model.completed"
MODEL_FAILED = "model.failed"
TOOL_STARTED = "tool.started"
TOOL_OUTPUT = "tool.output"
TOOL_COMPLETED = "tool.completed"
TOOL_FAILED = "tool.failed"
ARTIFACT_CREATED = "artifact.created"
MEMORY_RETRIEVED = "memory.retrieved"
MEMORY_WRITTEN = "memory.written"
RUN_CANCEL_REQUESTED = "run.cancel_requested"
RUN_CANCELLED = "run.cancelled"
RUN_COMPLETED = "run.completed"
RUN_FAILED = "run.failed"
TRIPWIRE = "safety.tripwire"
WARNING = "warning"
ANSWER_DELTA = "answer.delta"        # streamed chunk of the final answer (for live TTS)
ANSWER_CANCEL = "answer.cancel"      # streamed content turned out to be a tool step; discard
ACTION_CONFIRM_REQUESTED = "action.confirm_requested"  # a risky tool needs user approval
ACTION_CONFIRM_RESOLVED = "action.confirm_resolved"    # approval result (approved/denied)


class EventSink:
    """Async pub/sub with optional durable persistence.

    ``persist`` is an optional callback ``(Event) -> None`` (e.g. a DB write).
    Subscribers get an asyncio.Queue and receive every event emitted after they
    subscribe; ``replay`` (a callback returning stored events) lets a reconnecting
    client catch up by sequence number before live streaming.
    """

    def __init__(self, persist: Callable[[Event], None] | None = None) -> None:
        self._persist = persist
        self._seq: dict[str, int] = defaultdict(int)
        self._subs: set[asyncio.Queue[Event]] = set()
        self._counter = 0
        self._lock = asyncio.Lock()

    async def emit(
        self,
        run_id: str,
        type: str,
        payload: dict[str, Any] | None = None,
        thread_id: str | None = None,
    ) -> Event:
        async with self._lock:
            self._seq[run_id] += 1
            seq = self._seq[run_id]
            self._counter += 1
            eid = f"evt_{self._counter:08d}"
        ev = Event(
            id=eid,
            run_id=run_id,
            thread_id=thread_id,
            seq=seq,
            type=type,
            payload=payload or {},
        )
        if self._persist is not None:
            try:
                self._persist(ev)
            except Exception:  # persistence must never break a run, but must be visible
                logger.warning("event persistence failed for %s seq=%s", type, seq, exc_info=True)
        for q in list(self._subs):
            q.put_nowait(ev)
        return ev

    def seed_seq(self, run_id: str, last_seq: int) -> None:
        """Continue numbering after a restart/replay."""
        self._seq[run_id] = max(self._seq[run_id], last_seq)

    async def subscribe(self) -> "Subscription":
        q: asyncio.Queue[Event] = asyncio.Queue()
        self._subs.add(q)
        return Subscription(self, q)

    def _unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subs.discard(q)


class Subscription:
    def __init__(self, sink: EventSink, q: asyncio.Queue[Event]) -> None:
        self._sink = sink
        self._q = q

    async def stream(self) -> AsyncIterator[Event]:
        try:
            while True:
                yield await self._q.get()
        finally:
            self._sink._unsubscribe(self._q)

    def close(self) -> None:
        self._sink._unsubscribe(self._q)
