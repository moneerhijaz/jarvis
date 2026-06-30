"""PLANv5 §5 — the dataflow scheduler.

Runs a graph of tasks: a task starts only when every task it depends on is done; independent
ready tasks run concurrently; a task that needs another's output simply waits on it; results
accumulate so downstream/converge tasks can read them. Any task failure fails the whole run
immediately with a precise reason and cancels the rest (decision #1 — fail visibly, no
reconciliation). Recursion is handled by the task runner spawning a child run that shares the
same Budget; the scheduler only orchestrates.

Deliberately duck-typed (no imports from state.py) so the scheduling logic is unit-testable
with plain stub objects. It needs:
  pipe.tasks: dict[id, task]   pipe.results: dict[id, Result]   pipe.status / pipe.failure
  task.id / task.depends_on / task.state / task.intent / task.result / task.error
  run_task(task, pipe, budget) -> result   (async; result has .ok and .error)
  budget.spend_task() -> bool   budget.expired() -> bool   budget.why() -> str
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger("jarvis.pipeline.engine")

RunTask = Callable[[Any, Any, Any], Awaitable[Any]]


def _deps_done(pipe, task) -> bool:
    return all(pipe.tasks.get(d) is not None and pipe.tasks[d].state == "done"
               for d in task.depends_on)


def _ready(pipe) -> list:
    return [t for t in pipe.tasks.values() if t.state == "pending" and _deps_done(pipe, t)]


def _settled(pipe) -> bool:
    return all(t.state in ("done", "failed", "skipped") for t in pipe.tasks.values())


async def _cancel(running: dict) -> None:
    for fut in running.values():
        fut.cancel()
    if running:
        await asyncio.gather(*running.values(), return_exceptions=True)


async def run_pipeline(pipe, run_task: RunTask, budget,
                       is_cancelled: Callable[[], bool] | None = None) -> None:
    """Drive ``pipe.tasks`` to completion. Sets pipe.status/pipe.failure. Mutates each task's
    state/result as it goes. Tasks added DURING the run (recursion / replan) are picked up on
    the next scan, so the graph can grow while it executes."""
    is_cancelled = is_cancelled or (lambda: False)
    running: dict[str, asyncio.Future] = {}

    while True:
        if is_cancelled():
            pipe.status = "failed"
            pipe.failure = "cancelled"
            await _cancel(running)
            return
        if budget.expired():
            pipe.status = "failed"
            pipe.failure = f"engine: {budget.why()}"
            await _cancel(running)
            return

        # launch every ready task that isn't already running
        for t in _ready(pipe):
            if t.id in running:
                continue
            if not budget.spend_task():
                pipe.status = "failed"
                pipe.failure = f"engine: {budget.why()}"
                await _cancel(running)
                return
            t.state = "running"
            running[t.id] = asyncio.ensure_future(_run_one(t, pipe, run_task, budget))

        if not running:
            # nothing in flight: either everything is settled, or we're stuck on deps that
            # can never be satisfied (a planning bug) — fail visibly rather than hang.
            if _settled(pipe):
                break
            stuck = pipe.pending_ids() if hasattr(pipe, "pending_ids") else \
                [t.id for t in pipe.tasks.values() if t.state == "pending"]
            pipe.status = "failed"
            pipe.failure = f"engine: stuck — unmet dependencies for {stuck}"
            return

        done, _ = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
        for fut in done:
            tid = next(k for k, v in running.items() if v is fut)
            running.pop(tid, None)
            task = pipe.tasks[tid]
            try:
                res = fut.result()
            except asyncio.CancelledError:
                continue
            except Exception as e:                       # a task blew up -> fail the run
                logger.exception("task %s crashed", tid)
                task.state = "failed"
                task.error = str(e)
                pipe.status = "failed"
                pipe.failure = f"task '{tid}' ({task.intent}): {e}"
                await _cancel(running)
                return
            task.result = res
            if not getattr(res, "ok", True):             # task reported failure -> fail the run
                task.state = "failed"
                task.error = getattr(res, "error", None) or "failed"
                pipe.status = "failed"
                pipe.failure = f"task '{tid}' ({task.intent}): {task.error}"
                await _cancel(running)
                return
            task.state = "done"
            pipe.results[tid] = res

    if pipe.status == "running":
        pipe.status = "done"


async def _run_one(task, pipe, run_task: RunTask, budget):
    return await run_task(task, pipe, budget)
