"""PLANv5 §4 — the blackboard and task contracts.

One ``Pipe`` carries everything for a request. ``Task`` is one simple unit of work wired to
others by ``depends_on`` (the dataflow edges). ``Question`` is a Multiresponse ask that parks
the run until the client answers. ``Result`` is the small, finished piece a task contributes;
results converge into the answer. ``Budget`` bounds recursion/fan-out across the whole tree.

Pure data + light helpers — no model calls, no tool calls, no scheduling logic (that's the
engine). Reasoning is private to each task and never user-facing.
"""
from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

Level = Literal["direct", "multistep", "multiresponse"]
Verification = Literal["single", "double"]
TaskKind = Literal["leaf", "subtask", "compose", "ask", "pass"]
TaskState = Literal["pending", "running", "done", "failed", "skipped"]
RunStatus = Literal["running", "done", "failed", "refused", "awaiting_user"]


class Result(BaseModel):
    """The finished piece one task contributes. ``ok=False`` fails the run (decision #1)."""
    ok: bool = True
    kind: Literal["table", "list", "ranking", "scalar", "prose", "action", "none"] = "none"
    data: dict[str, Any] = Field(default_factory=dict)   # the typed payload (facts live here)
    text: str = ""                                       # rendered/spoken form, if any
    error: str | None = None
    source: str = ""                                     # which tool/model produced it (provenance)


class Question(BaseModel):
    """A Multiresponse ask. The engine parks the task until ``answer`` is set by the API.
    Unanswered => the run fails (decision #4); no default is ever assumed."""
    id: str
    task_id: str
    text: str
    answer: str | None = None
    answered: bool = False


class Task(BaseModel):
    """One simple unit of work. ``depends_on`` are the task ids whose results this needs;
    the engine won't start it until they're all done."""
    id: str
    kind: TaskKind = "leaf"
    intent: str = ""                          # the one simple thing this task does
    tool: str | None = None                   # the action it performs, if any
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)
    goal: str = ""                            # for subtask: the sub-request to recurse on
    question: str = ""                        # for ask: the question to pose
    state: TaskState = "pending"
    result: Result | None = None
    error: str | None = None
    scratch: str = ""                         # private reasoning — never surfaced


class Budget:
    """Shared across an entire request tree (parent + all recursive children) so fan-out and
    recursion can't run away. Not a pydantic model — it's mutable shared state."""

    def __init__(self, max_tasks: int = 40, max_depth: int = 2, deadline_s: int = 1800) -> None:
        self.max_tasks = max_tasks
        self.tasks_used = 0
        self.max_depth = max_depth
        self.deadline = time.time() + deadline_s

    def spend_task(self) -> bool:
        if self.tasks_used >= self.max_tasks:
            return False
        self.tasks_used += 1
        return True

    def depth_ok(self, depth: int) -> bool:
        return depth < self.max_depth

    def expired(self) -> bool:
        return time.time() > self.deadline

    def why(self) -> str:
        if self.expired():
            return "time budget exceeded"
        return f"task budget exceeded ({self.tasks_used}/{self.max_tasks})"


class Pipe(BaseModel):
    """The single state object for one request (the blackboard)."""
    model_config = {"arbitrary_types_allowed": True}

    run_id: str
    thread_id: str | None = None
    depth: int = 0                            # recursion depth (root = 0)

    # intake
    request: str
    normalized: str = ""
    history_context: str | None = None

    # feasibility ("does it have all necessary tools")
    feasible: bool = True
    missing: str = ""
    workaround: str = ""

    # dials
    verification: Verification = "single"
    objective: bool = False                   # has a single correct answer -> strict validate + double-verify
    grounded: bool = False                     # answer must reflect the tool evidence (e.g. a screen summary)
    concurrency: int = 1                       # v6: 1 (single) or 2 (double, brain+vision in parallel)
    tool_fail: int = 0                         # v6: feasibility re-check attempts used
    level: Level | None = None

    # the task graph + their outputs
    tasks: dict[str, Task] = Field(default_factory=dict)
    results: dict[str, Result] = Field(default_factory=dict)
    questions: list[Question] = Field(default_factory=list)

    # the one or two responses (single vs double)
    draft_a: Result | None = None
    draft_b: Result | None = None

    # output
    answer: str = ""
    artifacts: list[str] = Field(default_factory=list)
    status: RunStatus = "running"
    failure: str | None = None                # where + why it failed (decision #1)

    # -- helpers ---------------------------------------------------------- #
    def add(self, task: Task) -> Task:
        self.tasks[task.id] = task
        return task

    def deps_done(self, task: Task) -> bool:
        return all(self.tasks.get(d) and self.tasks[d].state == "done" for d in task.depends_on)

    def ready(self) -> list[Task]:
        return [t for t in self.tasks.values() if t.state == "pending" and self.deps_done(t)]

    def settled(self) -> bool:
        return all(t.state in ("done", "failed", "skipped") for t in self.tasks.values())

    def pending_ids(self) -> list[str]:
        return [t.id for t in self.tasks.values() if t.state == "pending"]

    def inputs_for(self, task: Task) -> dict[str, Result]:
        """The results this task depends on, keyed by upstream task id (the converge inputs)."""
        return {d: self.results[d] for d in task.depends_on if d in self.results}

    def fail(self, where: str, why: str) -> None:
        self.status = "failed"
        self.failure = f"{where}: {why}"
