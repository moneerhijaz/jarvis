"""End-to-end request scenarios: drive the WHOLE pipeline via ``PipelineRunner.run`` with a
prompt-routing fake model (no LM Studio, no network) and assert each request type responds
correctly. Complements test_pipeline_engine.py (which unit-tests individual stages).

Run: pytest tests/test_pipeline_requests.py -q
"""
import asyncio
from types import SimpleNamespace

from pydantic import BaseModel

from jarvis.events import EventSink
from jarvis.model.client import ModelResponse
from jarvis.pipeline.runner import PipelineRunner
from jarvis.pipeline.state import Result, Task  # noqa: F401  (kept for parity/readability)
from jarvis.tools.base import ToolError, ToolResult, ToolSpec


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #
class NoArgs(BaseModel):
    pass


def _spec(name: str) -> ToolSpec:
    return ToolSpec(name=name, namespace=name.split(".")[0], description=name,
                    args_model=NoArgs, handler=lambda a, c: ToolResult(ok=True),
                    output_kind="prose")


class Registry:
    """Minimal registry with a couple of non-vision tools (no ui.click, so the forced-click
    planner never fires and the model's plan is used)."""
    def __init__(self):
        self.specs = {"alpha.do": _spec("alpha.do"), "beta.do": _spec("beta.do")}

    def get(self, name):
        return self.specs.get(name)

    def names(self):
        return list(self.specs)

    def capability_index(self):
        return SimpleNamespace(capabilities=lambda: list(self.specs),
                               catalog=lambda names: "\n".join(names))


class OkExecutor:
    async def execute(self, call, ctx):
        return ToolResult(ok=True, data={"value": "hello world"})


def _settings(**over):
    auto = {"max_replans": 2, "max_retries": 3, "question_timeout_s": 600,
            "confirm_level": "never", "max_depth": 2, "verification": "single",
            "agreement": "facts"}
    auto.update(over)
    return SimpleNamespace(autonomy=SimpleNamespace(**auto),
                           models=SimpleNamespace(vision_base_url=""),
                           limits=SimpleNamespace(max_run_steps=40, max_wall_clock_s=60))


class RunModel:
    """Routes each model call by the stage's system-prompt marker, so it's robust to call
    order across the whole pipeline (classify / plan / bind / compose / validity / direct)."""
    provider = "fake"

    def __init__(self, classify, plan=None, direct="direct answer", compose="composed answer",
                 valid=True):
        self.classify = classify
        self.plan = plan or '{"tasks": []}'
        self.direct = direct
        self.compose = compose
        self.valid = valid

    def chat(self, messages, tools=None, **kwargs):
        s = messages[0].content
        if "Work out what this request NEEDS" in s:
            out = self.classify
        elif "Break the request into a small graph" in s:
            out = self.plan
        elif "Produce arguments for" in s:
            out = "{}"
        elif "Write the final answer" in s:
            out = self.compose
        elif "Judge whether the draft" in s:
            out = '{"valid": %s, "why": "ok"}' % ("true" if self.valid else "false")
        elif "Answer the user's request directly" in s:
            out = self.direct
        else:
            out = "unexpected-stage"
        return ModelResponse(content=out, provider="fake", model="fake")


def _req(run_id, message):
    return SimpleNamespace(run_id=run_id, message=message)


def _runner(model, executor=None):
    return PipelineRunner(_settings(), model, Registry(), executor, EventSink())


# --------------------------------------------------------------------------- #
# scenarios
# --------------------------------------------------------------------------- #
async def test_direct_objective_answers_exactly():
    """'what is 2+2' -> direct, objective; strict validity passes; answer is the model's."""
    m = RunModel('{"needs_tools": false, "feasible": true, "needs_user_info": false, '
                 '"objective": true, "grounded": false}', direct="4", valid=True)
    res = await _runner(m).run(_req("d1", "what is 2+2"))
    assert res.status == "completed"
    assert res.final_answer == "4"


async def test_open_paragraph_is_not_validation_failed():
    """'give me a random paragraph' -> direct, OPEN (subjective). The validity gate must accept
    any substantive answer — the old bug failed it after 3 redrafts."""
    para = "A quiet morning settled over the harbor as gulls traced lazy arcs above the boats."
    m = RunModel('{"needs_tools": false, "feasible": true, "needs_user_info": false, '
                 '"objective": false, "grounded": false}', direct=para, valid=False)  # valid=False must NOT matter
    res = await _runner(m).run(_req("d2", "give me a random paragraph"))
    assert res.status == "completed"
    assert res.final_answer == para          # accepted as-is, never redrafted/failed


async def test_multistep_grounded_runs_tool_and_composes():
    """A tool request -> multistep, grounded; runs the planned tool and composes an answer that
    the grounded validity check accepts."""
    m = RunModel('{"needs_tools": true, "feasible": true, "needs_user_info": false, '
                 '"objective": false, "grounded": true}',
                 plan='{"tasks": [{"id": "t1", "intent": "do beta", "tool": "beta.do", '
                      '"depends_on": []}]}',
                 compose="Here is the summary: hello world", valid=True)
    res = await _runner(m, OkExecutor()).run(_req("m1", "summarize the thing for me"))
    assert res.status == "completed"
    assert "hello world" in res.final_answer


async def test_infeasible_delivers_refusal_as_answer():
    """Impossible request, no workaround -> refuse; the failure is delivered as the answer."""
    m = RunModel('{"feasible": false, "missing": "physical hardware access", '
                 '"workaround": "none", "needs_tools": true}')
    res = await _runner(m).run(_req("i1", "physically replace my GPU"))
    assert res.status == "completed"          # a refusal is still a delivered answer
    low = res.final_answer.lower()
    assert "physical hardware access" in low and "can" in low   # "I don't think I can…"


async def test_multiresponse_asks_then_completes_on_answer():
    """'write the email' (no recipient) -> multiresponse: it asks, parks, and completes once the
    client answers."""
    m = RunModel('{"needs_tools": false, "feasible": true, "needs_user_info": true, '
                 '"objective": false, "grounded": false}',
                 plan='{"tasks": [{"id": "q1", "kind": "ask", '
                      '"question": "Who is the recipient?", "depends_on": []}]}',
                 compose="Email drafted to the recipient.", valid=True)
    runner = _runner(m)

    async def answerer():
        for _ in range(200):
            await asyncio.sleep(0.01)
            if runner._questions:
                qid = next(iter(runner._questions))
                runner.answer_question(qid, "Bob")
                return
        raise AssertionError("run never asked a question")

    res, _ = await asyncio.gather(runner.run(_req("mr1", "write the email")), answerer())
    assert res.status == "completed"
    assert "drafted" in res.final_answer.lower()


async def test_unanswered_question_fails_visibly():
    """Decision #4: a Multiresponse question that is cancelled (not answered) fails the run with
    a reason rather than guessing."""
    m = RunModel('{"needs_tools": false, "feasible": true, "needs_user_info": true, '
                 '"objective": false, "grounded": false}',
                 plan='{"tasks": [{"id": "q1", "kind": "ask", '
                      '"question": "Which file?", "depends_on": []}]}')
    runner = _runner(m)

    async def canceller():
        for _ in range(200):
            await asyncio.sleep(0.01)
            if runner._questions:
                runner.request_cancel("u1")   # unblocks the parked question with no answer
                return

    res, _ = await asyncio.gather(runner.run(_req("u1", "open the file")), canceller())
    assert res.status in ("failed", "cancelled")
    assert res.final_answer is None or "couldn" in (res.final_answer or "").lower() or res.error
