"""Phase 2 fix — 'remember X' must deterministically plan a single vault.capture (observed live:
the LLM planner otherwise mis-plans it as reading a nonexistent profile note and fails). Offline."""
from types import SimpleNamespace

from pydantic import BaseModel

from jarvis.events import EventSink
from jarvis.pipeline.runner import PipelineRunner
from jarvis.pipeline.state import Pipe
from jarvis.tools.base import ToolResult, ToolSpec


class NoArgs(BaseModel):
    pass


def _spec(name):
    return ToolSpec(name=name, namespace=name.split(".")[0], description=name,
                    args_model=NoArgs, handler=lambda a, c: ToolResult(ok=True), output_kind="action")


class Reg:
    def __init__(self):
        self.specs = {"vault.capture": _spec("vault.capture")}

    def get(self, n):
        return self.specs.get(n)

    def names(self):
        return list(self.specs)

    def capability_index(self):
        return SimpleNamespace(capabilities=lambda: list(self.specs), catalog=lambda ns: "")


def _settings():
    return SimpleNamespace(
        vault=SimpleNamespace(auto_extract=True),
        autonomy=SimpleNamespace(max_retries=3, verification="single", question_timeout_s=600,
                                 confirm_level="never", max_depth=2, agreement="facts", max_replans=2),
        models=SimpleNamespace(vision_base_url=""),
        limits=SimpleNamespace(max_run_steps=40, max_wall_clock_s=60))


def _runner():
    return PipelineRunner(_settings(), SimpleNamespace(provider="fake"), Reg(), None, EventSink())


def test_memory_text_strips_imperative():
    r = _runner()
    assert r._memory_text("Remember, I'm building a Windows assistant called BlueFalcon") \
        == "I'm building a Windows assistant called BlueFalcon"
    assert r._memory_text("please save that I prefer tea") == "I prefer tea"


def test_force_capture_plan_builds_single_capture():
    r = _runner()
    pipe = Pipe(run_id="r1", request="Remember, I'm building BlueFalcon")
    pipe.normalized = pipe.request
    assert r._force_capture_plan(pipe) is True
    t1 = pipe.tasks["t1"]
    assert t1.tool == "vault.capture"
    assert t1.args["text"] == "I'm building BlueFalcon"
    assert "compose" in pipe.tasks


def test_no_capture_plan_for_normal_request():
    r = _runner()
    pipe = Pipe(run_id="r2", request="what is the weather today")
    pipe.normalized = pipe.request
    assert r._force_capture_plan(pipe) is False
