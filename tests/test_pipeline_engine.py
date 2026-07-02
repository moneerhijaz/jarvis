"""PLANv6 hardening tests: scheduler watchdog, cycle detection, bounded re-planning,
ask-question timeout, and the facts comparison fixes.

Pure-logic tests — no LM Studio, no network. Run: pytest tests/test_pipeline_engine.py -q
"""
import asyncio
import time
from types import SimpleNamespace

from pydantic import BaseModel

from jarvis.events import EventSink
from jarvis.model.client import ModelResponse
from jarvis.pipeline.engine import find_cycle, run_pipeline
from jarvis.pipeline.facts import facts_agree, ungrounded_facts
from jarvis.pipeline.runner import PipelineRunner
from jarvis.pipeline.state import Budget, Pipe, Result, Task
from jarvis.tools.base import ToolContext, ToolError, ToolResult, ToolSpec


# --------------------------------------------------------------------------- #
# engine: scheduler
# --------------------------------------------------------------------------- #

def _pipe(*tasks: Task) -> Pipe:
    p = Pipe(run_id="t", request="test")
    for t in tasks:
        p.add(t)
    return p


async def _ok_runner(task, pipe, budget):
    return Result(ok=True, kind="prose", text=task.id)


async def test_deps_and_parallelism():
    order: list[tuple[str, str]] = []

    async def run(task, pipe, budget):
        order.append(("start", task.id))
        await asyncio.sleep(0.05)
        order.append(("end", task.id))
        return Result(ok=True)

    p = _pipe(Task(id="a"), Task(id="b"), Task(id="c", depends_on=["a", "b"]))
    await run_pipeline(p, run, Budget())
    assert p.status == "done"
    assert order.index(("end", "a")) < order.index(("start", "c"))
    assert order.index(("end", "b")) < order.index(("start", "c"))
    # a and b overlap (parallel), c strictly after
    assert order.index(("start", "b")) < order.index(("end", "a"))


async def test_failure_fails_run_and_cancels_siblings():
    cancelled = {}

    async def run(task, pipe, budget):
        if task.id == "bad":
            await asyncio.sleep(0.02)
            return Result(ok=False, error="boom")
        try:
            await asyncio.sleep(10)
            return Result(ok=True)
        except asyncio.CancelledError:
            cancelled[task.id] = True
            raise

    p = _pipe(Task(id="bad"), Task(id="slow"))
    t0 = time.time()
    await run_pipeline(p, run, Budget())
    assert p.status == "failed" and "boom" in p.failure
    assert cancelled.get("slow") and time.time() - t0 < 3


async def test_watchdog_enforces_deadline_mid_flight():
    """A hung task can no longer freeze the run past its deadline (the old engine awaited
    task completion with no timeout, so the deadline was only checked between completions)."""
    async def run(task, pipe, budget):
        await asyncio.sleep(60)
        return Result(ok=True)

    p = _pipe(Task(id="hang"))
    t0 = time.time()
    await run_pipeline(p, run, Budget(deadline_s=1))
    assert p.status == "failed" and "time budget" in p.failure
    assert time.time() - t0 < 5


async def test_watchdog_observes_cancel_mid_flight():
    flag = {"c": False}

    async def run(task, pipe, budget):
        await asyncio.sleep(60)
        return Result(ok=True)

    async def trip():
        await asyncio.sleep(0.3)
        flag["c"] = True

    p = _pipe(Task(id="hang"))
    t0 = time.time()
    await asyncio.gather(run_pipeline(p, run, Budget(), lambda: flag["c"]), trip())
    assert p.status == "failed" and p.failure == "cancelled"
    assert time.time() - t0 < 5


async def test_cycle_detected_up_front():
    p = _pipe(Task(id="a", depends_on=["b"]), Task(id="b", depends_on=["a"]))
    assert find_cycle(p)
    await run_pipeline(p, _ok_runner, Budget())
    assert p.status == "failed" and "cycle" in p.failure


async def test_unmet_dependency_fails_visibly():
    p = _pipe(Task(id="a", depends_on=["ghost"]))
    await run_pipeline(p, _ok_runner, Budget())
    assert p.status == "failed" and "stuck" in p.failure


async def test_task_budget_stops_run():
    p = _pipe(*[Task(id=f"t{i}") for i in range(10)])
    await run_pipeline(p, _ok_runner, Budget(max_tasks=3))
    assert p.status == "failed" and "task budget" in p.failure


async def test_tasks_added_mid_run_are_executed():
    async def run(task, pipe, budget):
        if task.id == "a" and "new" not in pipe.tasks:
            pipe.add(Task(id="new"))
        return Result(ok=True)

    p = _pipe(Task(id="a"))
    await run_pipeline(p, run, Budget())
    assert p.status == "done" and p.tasks["new"].state == "done"


# --------------------------------------------------------------------------- #
# runner: bounded re-planning + ask timeout
# --------------------------------------------------------------------------- #

class NoArgs(BaseModel):
    pass


def _spec(name: str) -> ToolSpec:
    return ToolSpec(name=name, namespace=name.split(".")[0], description=name,
                    args_model=NoArgs, handler=lambda a, c: ToolResult(ok=True),
                    output_kind="prose")


class TwoToolRegistry:
    def __init__(self):
        self.specs = {"alpha.do": _spec("alpha.do"), "beta.do": _spec("beta.do")}

    def get(self, name):
        return self.specs.get(name)

    def names(self):
        return list(self.specs)

    def capability_index(self):
        return SimpleNamespace(capabilities=lambda: list(self.specs),
                               catalog=lambda names: "\n".join(names))


class AlphaFailsExecutor:
    """alpha.do always fails (non-repairable); beta.do succeeds."""

    def __init__(self):
        self.calls = []

    async def execute(self, call, ctx):
        self.calls.append(call.name)
        if call.name == "alpha.do":
            return ToolResult(ok=False, error=ToolError(
                code="tool_error", message="alpha exploded", category="execution", retryable=False))
        return ToolResult(ok=True, data={"value": "beta says hi"})


class ReplanModel:
    """Scripted: the re-plan prompt gets a beta.do plan; everything else gets prose/empty JSON."""
    provider = "fake"

    def chat(self, messages, tools=None, **kwargs):
        sysp = messages[0].content
        if "failed partway" in sysp:
            return ModelResponse(
                content='{"tasks":[{"id":"r1","intent":"do it with beta","tool":"beta.do","depends_on":[]}]}',
                provider="fake", model="replan")
        if "Produce arguments" in sysp:
            return ModelResponse(content="{}", provider="fake", model="args")
        return ModelResponse(content="All done: beta says hi", provider="fake", model="compose")


def _settings(**over):
    auto = {"max_replans": 2, "max_retries": 3, "question_timeout_s": 600,
            "confirm_level": "never", "max_depth": 2, "verification": "single",
            "agreement": "facts"}
    auto.update(over)
    return SimpleNamespace(autonomy=SimpleNamespace(**auto),
                           models=SimpleNamespace(vision_base_url=""),
                           limits=SimpleNamespace(max_run_steps=40, max_wall_clock_s=60))


async def test_step_failure_triggers_replan_and_recovers():
    registry = TwoToolRegistry()
    executor = AlphaFailsExecutor()
    runner = PipelineRunner(_settings(), ReplanModel(), registry, executor, EventSink())
    pipe = Pipe(run_id="r1", request="do the thing")
    pipe.normalized = pipe.request
    pipe.add(Task(id="t1", kind="leaf", intent="try alpha", tool="alpha.do"))
    pipe.add(Task(id="compose", kind="compose", intent="compose the answer", depends_on=["t1"]))
    runner._contexts[pipe.run_id] = ToolContext(run_id=pipe.run_id, working_directory=None,
                                                settings=runner.settings)
    req = SimpleNamespace(run_id=pipe.run_id)

    ok = await runner._execute_with_replan(pipe, ReplanModel(), Budget(), req)

    assert ok, pipe.failure
    assert "alpha.do" in executor.calls and "beta.do" in executor.calls
    assert pipe.results["r1"].ok                      # replanned step ran
    assert pipe.results["compose"].ok                 # answer converged after recovery


async def test_replans_are_bounded_then_fail_visibly():
    registry = TwoToolRegistry()

    class AllFailExecutor:
        async def execute(self, call, ctx):
            return ToolResult(ok=False, error=ToolError(
                code="tool_error", message="nope", category="execution", retryable=False))

    class AlwaysAlphaModel(ReplanModel):
        def chat(self, messages, tools=None, **kwargs):
            sysp = messages[0].content
            if "failed partway" in sysp:
                return ModelResponse(
                    content='{"tasks":[{"id":"t1","intent":"retry alpha","tool":"alpha.do","depends_on":[]}]}',
                    provider="fake", model="replan")
            return super().chat(messages, tools, **kwargs)

    runner = PipelineRunner(_settings(max_replans=2), AlwaysAlphaModel(), registry,
                            AllFailExecutor(), EventSink())
    pipe = Pipe(run_id="r2", request="do the thing")
    pipe.normalized = pipe.request
    pipe.add(Task(id="t1", kind="leaf", intent="try alpha", tool="alpha.do"))
    pipe.add(Task(id="compose", kind="compose", intent="compose", depends_on=["t1"]))
    runner._contexts[pipe.run_id] = ToolContext(run_id=pipe.run_id, working_directory=None,
                                                settings=runner.settings)
    ok = await runner._execute_with_replan(pipe, AlwaysAlphaModel(), Budget(),
                                           SimpleNamespace(run_id=pipe.run_id))
    assert not ok
    assert pipe.status == "failed" and "nope" in pipe.failure


async def test_user_denial_is_not_replanned():
    runner = PipelineRunner(_settings(), ReplanModel(), TwoToolRegistry(), None, EventSink())
    assert not runner._replannable("task 't1' (x): denied by user")
    assert not runner._replannable("cancelled")
    assert not runner._replannable("engine: time budget exceeded")
    assert not runner._replannable("question was not answered within 600s")
    assert runner._replannable("task 't1' (x): alpha.do: file not found")


async def test_ingest_plan_renames_colliding_ids():
    runner = PipelineRunner(_settings(), ReplanModel(), TwoToolRegistry(), None, EventSink())
    pipe = Pipe(run_id="r3", request="x")
    done = Task(id="t1", kind="leaf", intent="done already", tool="alpha.do")
    done.state = "done"
    pipe.add(done)
    pipe.results["t1"] = Result(ok=True, text="prior")
    added = runner._ingest_plan(
        pipe,
        {"tasks": [{"id": "t1", "intent": "new step", "tool": "beta.do", "depends_on": ["t1"]}]},
        id_suffix="_r1")
    assert added == {"t1_r1"}
    assert pipe.tasks["t1"].state == "done"                 # completed work preserved
    assert pipe.tasks["t1_r1"].depends_on == []             # no self-dep from the rename
    assert set(pipe.tasks["compose"].depends_on) == {"t1", "t1_r1"}
    assert find_cycle(pipe) is None


async def test_unanswered_question_times_out_instead_of_hanging():
    runner = PipelineRunner(_settings(question_timeout_s=1), ReplanModel(),
                            TwoToolRegistry(), None, EventSink())
    pipe = Pipe(run_id="r4", request="x")
    task = Task(id="q1", kind="ask", intent="ask", question="what file?")
    t0 = time.time()
    res = await runner._exec_ask(task, pipe)
    assert not res.ok and "not answered" in res.error
    assert time.time() - t0 < 5


async def test_answered_question_resumes():
    runner = PipelineRunner(_settings(question_timeout_s=30), ReplanModel(),
                            TwoToolRegistry(), None, EventSink())
    pipe = Pipe(run_id="r5", request="x")
    task = Task(id="q1", kind="ask", intent="ask", question="what file?")

    async def answer_soon():
        await asyncio.sleep(0.1)
        qid = pipe.questions[0].id
        assert runner.answer_question(qid, "notes.txt")

    res, _ = await asyncio.gather(runner._exec_ask(task, pipe), answer_soon())
    assert res.ok and res.text == "notes.txt"
    assert pipe.questions[0].answered


# --------------------------------------------------------------------------- #
# v7: split classify, workaround pass, routing fix, parallel double, 1-regen match
# --------------------------------------------------------------------------- #

class ScriptedJSONModel:
    """Returns the scripted replies in order; records every system prompt it saw."""
    provider = "fake"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, messages, tools=None, **kwargs):
        self.calls.append(messages[0].content)
        content = self.replies.pop(0) if self.replies else "{}"
        return ModelResponse(content=content, provider="fake", model="scripted")


class ChatModel:
    provider = "fake"

    def __init__(self, text):
        self.text = text

    def chat(self, messages, tools=None, **kwargs):
        return ModelResponse(content=self.text, provider="fake", model="chat")


async def test_needs_user_info_forces_multiresponse_even_without_tools():
    """v7 routing fix: 'write the email' (no tools, but needs the recipient) must route to
    multiresponse — previously it routed direct, which cannot ask."""
    model = ScriptedJSONModel(['{"needs_tools": false, "feasible": true, '
                               '"needs_user_info": true, "objective": false, "grounded": false}'])
    runner = PipelineRunner(_settings(), model, TwoToolRegistry(), None, EventSink())
    pipe = Pipe(run_id="c1", request="write the email")
    pipe.normalized = pipe.request
    await runner._classify(pipe, model)
    assert pipe.level == "multiresponse"


async def test_classify_no_longer_drafts_the_answer():
    model = ScriptedJSONModel(['{"needs_tools": false, "feasible": true, '
                               '"needs_user_info": false, "objective": true, "grounded": false}'])
    runner = PipelineRunner(_settings(), model, TwoToolRegistry(), None, EventSink())
    pipe = Pipe(run_id="c2", request="what is 2+2")
    pipe.normalized = pipe.request
    await runner._classify(pipe, model)
    assert pipe.level == "direct"
    assert pipe.draft_a is None                       # drafting happens in the module now
    assert "Do NOT answer the request itself" in model.calls[0]


async def test_infeasible_with_workaround_gets_one_informed_recheck():
    model = ScriptedJSONModel([
        '{"feasible": false, "missing": "a pdf export tool", "workaround": "shell", "needs_tools": true}',
        '{"feasible": true, "needs_tools": true, "needs_user_info": false, '
        '"objective": false, "grounded": true}',
    ])
    runner = PipelineRunner(_settings(), model, TwoToolRegistry(), None, EventSink())
    pipe = Pipe(run_id="w1", request="export this page as pdf")
    pipe.normalized = pipe.request
    ok = await runner._check_feasible(pipe, model)
    assert ok and pipe.level == "multistep"
    assert len(model.calls) == 2
    assert "shell commands" in model.calls[1]         # the retry carried NEW information


async def test_infeasible_without_workaround_refuses_after_one_check():
    model = ScriptedJSONModel(['{"feasible": false, "missing": "physical hardware access", '
                               '"workaround": "none", "needs_tools": true}'])
    runner = PipelineRunner(_settings(), model, TwoToolRegistry(), None, EventSink())
    pipe = Pipe(run_id="w2", request="replace my GPU")
    pipe.normalized = pipe.request
    ok = await runner._check_feasible(pipe, model)
    assert not ok
    assert len(model.calls) == 1                      # no blind re-checks


async def test_workaround_that_still_fails_is_not_offered_in_refusal():
    model = ScriptedJSONModel([
        '{"feasible": false, "missing": "x", "workaround": "web", "needs_tools": true}',
        '{"feasible": false, "missing": "x", "workaround": "web", "needs_tools": true}',
    ])
    runner = PipelineRunner(_settings(), model, TwoToolRegistry(), None, EventSink())
    pipe = Pipe(run_id="w3", request="do x")
    pipe.normalized = pipe.request
    ok = await runner._check_feasible(pipe, model)
    assert not ok and pipe.workaround == "none"
    assert "search the web" not in runner._refusal(pipe)


async def test_match_regenerates_once_then_fails_showing_both():
    runner = PipelineRunner(_settings(), ChatModel("x"), TwoToolRegistry(), None, EventSink())
    runner._vision_model = lambda: object()           # a second host exists
    calls = []

    async def fake_second(pipe, vmodel):
        calls.append(1)
        return Result(ok=True, kind="prose", text="the answer is 5", source="vision")

    runner._second_pass = fake_second
    pipe = Pipe(run_id="m1", request="2+2?")
    pipe.normalized = pipe.request
    pipe.verification = "double"
    pipe.objective = True
    pipe.draft_a = Result(ok=True, kind="prose", text="4", source="brain")

    ok = await runner._match_passes(pipe)
    assert not ok
    assert len(calls) == 2                            # initial + exactly ONE regeneration
    assert pipe.status == "failed"
    assert "[A] 4" in pipe.failure and "5" in pipe.failure   # both shown


async def test_match_uses_parallel_draft_b_without_regenerating():
    runner = PipelineRunner(_settings(), ChatModel("x"), TwoToolRegistry(), None, EventSink())
    runner._vision_model = lambda: object()

    async def fake_second(pipe, vmodel):
        raise AssertionError("second pass must not be regenerated when drafts agree")

    runner._second_pass = fake_second
    pipe = Pipe(run_id="m2", request="2+2?")
    pipe.normalized = pipe.request
    pipe.verification = "double"
    pipe.objective = True
    pipe.draft_a = Result(ok=True, kind="prose", text="4", source="brain")
    pipe.draft_b = Result(ok=True, kind="prose", text="two plus two equals four", source="vision")
    assert await runner._match_passes(pipe)


async def test_compose_fires_brain_and_vision_in_parallel_when_double():
    runner = PipelineRunner(_settings(), ChatModel("brain says 4"), TwoToolRegistry(),
                            None, EventSink())
    runner._vision_model = lambda: ChatModel("vision says 4")
    pipe = Pipe(run_id="p1", request="count them")
    pipe.normalized = pipe.request
    pipe.verification = "double"
    pipe.objective = True
    pipe.results["t1"] = Result(ok=True, kind="scalar", data={"count": 4}, source="beta.do")
    task = Task(id="compose", kind="compose", intent="compose", depends_on=["t1"])

    res = await runner._exec_compose(task, pipe)
    assert res.ok and res.text == "brain says 4" and res.source == "brain"
    assert pipe.draft_b is not None
    assert pipe.draft_b.text == "vision says 4" and pipe.draft_b.source == "vision"


async def test_compose_stays_single_when_not_objective():
    runner = PipelineRunner(_settings(), ChatModel("brain says things"), TwoToolRegistry(),
                            None, EventSink())
    runner._vision_model = lambda: (_ for _ in ()).throw(AssertionError("must not resolve vision"))
    pipe = Pipe(run_id="p2", request="summarize")
    pipe.normalized = pipe.request
    pipe.verification = "double"
    pipe.objective = False                            # subjective -> nothing to cross-check
    pipe.results["t1"] = Result(ok=True, kind="prose", data={"text": "stuff"}, source="beta.do")
    task = Task(id="compose", kind="compose", intent="compose", depends_on=["t1"])

    res = await runner._exec_compose(task, pipe)
    assert res.ok and pipe.draft_b is None


# --------------------------------------------------------------------------- #
# facts: agreement + grounding fixes
# --------------------------------------------------------------------------- #

def test_number_words_agree_with_digits():
    assert facts_agree("2+2 equals four", "4")[0]


def test_conflicting_numbers_disagree():
    assert not facts_agree("the answer is 4", "the answer is 5")[0]


def test_factless_pass_does_not_vouch_for_factful_one():
    """Regression: an answer with zero extractable facts was a subset of anything, so
    double-verification auto-passed. It must fall through to key-term comparison instead."""
    agree, detail = facts_agree("the answer is definitely correct", "the count is 4")
    assert not agree, detail


def test_windows_path_styles_agree():
    assert facts_agree("saved to C:\\a\\b.txt", "I saved it to c:/a/b.txt for you")[0]


def test_ungrounded_path_detected():
    assert ungrounded_facts("it's at C:\\fake\\path.txt", "tool output: C:\\real\\file.txt")
    assert not ungrounded_facts("it's at C:\\real\\file.txt", "tool output: C:\\real\\file.txt")
