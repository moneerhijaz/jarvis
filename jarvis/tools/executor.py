"""Tool execution pipeline (PLANv3 / PLANv2 §7.5).

Validates arguments, runs the handler in a worker thread with a timeout, captures
the result, writes an audit entry (with redaction), persists the tool call, and
emits tool.started / tool.completed|failed events. The agent loop calls this; the
loop, not the tool, owns event emission and persistence.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from pydantic import ValidationError

logger = logging.getLogger("jarvis.tools.executor")

from jarvis.events import EventSink, TOOL_COMPLETED, TOOL_FAILED, TOOL_STARTED
from jarvis.model.client import ToolCall
from jarvis.security.redaction import redact_obj
from jarvis.tools.base import Registry, ToolContext, ToolError, ToolResult


class ToolExecutor:
    def __init__(self, registry: Registry, events: EventSink, store=None) -> None:
        self.registry = registry
        self.events = events
        self.store = store

    async def execute(self, call: ToolCall, ctx: ToolContext) -> ToolResult:
        spec = self.registry.get(call.name)
        # Models sometimes emit "ui_click" instead of "ui.click"; normalize underscore->dot.
        if spec is None and "." not in call.name and "_" in call.name:
            alt = call.name.replace("_", ".", 1)
            alt_spec = self.registry.get(alt)
            if alt_spec is not None:
                call.name, spec = alt, alt_spec
        tc_id = call.id or f"call_{uuid.uuid4().hex[:8]}"
        started = time.time()

        if spec is None:
            res = ToolResult(
                ok=False,
                error=ToolError(code="unknown_tool", message=f"No tool named {call.name}", category="not_found"),
            )
            await self._finish(call.name, tc_id, ctx, {}, res, started)
            return res

        # validate arguments
        try:
            args = spec.args_model(**call.arguments)
        except ValidationError as e:
            res = ToolResult(
                ok=False,
                error=ToolError(code="bad_args", message=str(e), category="validation", retryable=True),
            )
            await self._finish(call.name, tc_id, ctx, call.arguments, res, started)
            return res

        await self.events.emit(
            ctx.run_id, TOOL_STARTED,
            {"tool_call_id": tc_id, "tool_name": call.name, "arguments": redact_obj(call.arguments), "risk": spec.risk},
        )

        # run handler in a thread with timeout
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(spec.handler, args, ctx),
                timeout=spec.timeout_s,
            )
        except asyncio.TimeoutError:
            self._kill_processes(ctx)
            res = ToolResult(
                ok=False,
                error=ToolError(code="timeout", message=f"{call.name} exceeded {spec.timeout_s}s", category="timeout"),
            )
        except Exception as e:  # handler crash -> structured error
            res = ToolResult(
                ok=False,
                error=ToolError(code="handler_error", message=str(e), category="execution"),
            )

        res.duration_ms = res.duration_ms or int((time.time() - started) * 1000)
        await self._finish(call.name, tc_id, ctx, call.arguments, res, started, risk=spec.risk)
        return res

    def _kill_processes(self, ctx: ToolContext) -> None:
        for p in list(ctx.processes):
            try:
                p.kill()
            except Exception:
                pass

    async def _finish(self, name, tc_id, ctx, arguments, res: ToolResult, started, risk="read") -> None:
        # persist tool call + audit
        if self.store is not None:
            try:
                # Vision screen tools carry on-screen content we don't
                # want written verbatim to the local audit log.
                stored_data = ({"_redacted": True, "keys": list(res.data.keys())}
                               if res.redact_data else redact_obj(res.data))
                self.store.add_tool_call({
                    "id": tc_id, "run_id": ctx.run_id, "tool_name": name,
                    "arguments": redact_obj(arguments),
                    "result": {"ok": res.ok, "data": stored_data, "error": res.error.model_dump() if res.error else None},
                    "status": "completed" if res.ok else "failed",
                    "started_at": started, "ended_at": time.time(),
                    "duration_ms": res.duration_ms, "error_code": res.error.code if res.error else None,
                })
                self.store.add_audit({
                    "id": f"aud_{uuid.uuid4().hex[:10]}", "run_id": ctx.run_id, "tool_call_id": tc_id,
                    "action_type": name, "target": str(arguments.get("path") or arguments.get("command") or ""),
                    "summary": ("ok" if res.ok else f"error:{res.error.code}") if True else "",
                    "risk": risk, "arguments_redacted": redact_obj(arguments),
                    "result_summary": {"ok": res.ok}, "rollback_token": res.rollback_token,
                })
                for art in res.artifacts:
                    self.store.add_artifact({
                        "id": art.id or f"art_{uuid.uuid4().hex[:8]}", "run_id": ctx.run_id,
                        "tool_call_id": tc_id, "type": art.type, "path": art.path,
                        "mime_type": art.mime_type, "size_bytes": art.size_bytes,
                    })
            except Exception:
                logger.warning("failed to persist tool_call/audit for %s", name, exc_info=True)

        ev_type = TOOL_COMPLETED if res.ok else TOOL_FAILED
        await self.events.emit(ctx.run_id, ev_type, {
            "tool_call_id": tc_id, "tool_name": name, "ok": res.ok,
            "duration_ms": res.duration_ms,
            "error": res.error.model_dump() if res.error else None,
            "rollback_token": res.rollback_token,
            "artifacts": [a.path for a in res.artifacts],
        })
