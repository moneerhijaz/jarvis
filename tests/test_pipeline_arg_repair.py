import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

from pydantic import BaseModel

from jarvis.events import EventSink
from jarvis.model.client import ModelResponse
from jarvis.pipeline.runner import PipelineRunner
from jarvis.pipeline.state import Pipe, Result, Task
from jarvis.tools.base import ToolContext, ToolError, ToolResult, ToolSpec


class MatchArgs(BaseModel):
    match: str


class ClickArgs(BaseModel):
    target: str
    window: str | None = None


class RepairModel:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(content="{}", provider="fake", model="repair")
        return ModelResponse(
            content='{"why":"dummy.action needs the required match field","arguments":{"match":"fixed-value"}}',
            provider="fake",
            model="repair",
        )


class NoToolsModel:
    provider = "fake"

    def chat(self, messages, tools=None, **kwargs):
        return ModelResponse(
            content=(
                '{"needs_tools": false, "feasible": true, "missing": "", "workaround": "none", '
                '"needs_user_info": false, "objective": false, "grounded": false, '
                '"answer": "I am unable to locate the specific YouTube tab."}'
            ),
            provider="fake",
            model="no-tools",
        )


class FakeRegistry:
    def __init__(self, spec):
        self.spec = spec

    def get(self, name):
        return self.spec if name == self.spec.name else None


class FakeCapabilityIndex:
    def capabilities(self):
        return ["ui.click", "screen.look"]


class ClassifyRegistry(FakeRegistry):
    def capability_index(self):
        return FakeCapabilityIndex()


class RepairingExecutor:
    def __init__(self):
        self.calls = []

    async def execute(self, call, ctx):
        self.calls.append(call.arguments)
        if len(self.calls) == 1:
            return ToolResult(
                ok=False,
                error=ToolError(
                    code="bad_args",
                    message="1 validation error for MatchArgs\nmatch\n  Field required",
                    category="validation",
                    retryable=True,
                ),
            )
        return ToolResult(ok=True, data={"value": call.arguments["match"]})


@dataclass
class DummySettings:
    pass


class ClassifySettings:
    autonomy = SimpleNamespace(verification="single")
    models = SimpleNamespace(vision_base_url="")


def test_leaf_tool_repairs_validation_error_from_prior_result():
    spec = ToolSpec(
        name="dummy.action",
        namespace="dummy",
        description="Action requiring a match value.",
        args_model=MatchArgs,
        handler=lambda args, ctx: ToolResult(ok=True),
        output_kind="action",
    )
    executor = RepairingExecutor()
    events = []

    async def run():
        sink = EventSink(persist=lambda event: events.append(event))
        runner = PipelineRunner(
            DummySettings(),
            RepairModel(),
            FakeRegistry(spec),
            executor,
            sink,
        )
        pipe = Pipe(run_id="run_repair", request="Run the dummy action.")
        pipe.normalized = "Run the dummy action."
        pipe.results["t1"] = Result(
            ok=True,
            kind="list",
            source="dummy.source",
            data={"hint": "fixed-value"},
        )
        task = Task(id="t2", kind="leaf", intent="Run the dummy action", tool="dummy.action", depends_on=["t1"])
        runner._contexts[pipe.run_id] = ToolContext(run_id=pipe.run_id, working_directory=None, settings=DummySettings())

        result = await runner._exec_leaf(task, pipe)
        return result

    result = asyncio.run(run())

    assert result.ok
    assert executor.calls[0] == {}
    assert executor.calls[1] == {"match": "fixed-value"}
    thoughts = [event for event in events if event.type == "model.thought"]
    assert thoughts
    assert "repairing (1/3)" in thoughts[0].payload["content"]
    assert thoughts[0].payload["repaired_args"] == {"match": "fixed-value"}


def test_default_registry_excludes_non_vision_monitor_shortcuts():
    from jarvis.tools import build_default_registry

    reg = build_default_registry()
    forbidden = {
        "screen.capture",
        "screen.regions",
        "win.list",
        "win.focus",
        "win.move",
        "win.state",
        "input.position",
    }

    assert forbidden.isdisjoint(set(reg.names()))
    assert {"screen.look", "screen.read_text", "ui.find", "ui.click"}.issubset(set(reg.names()))


def test_visible_click_request_forces_ui_click_plan():
    spec = ToolSpec(
        name="ui.click",
        namespace="ui",
        description="Click a visible screen target.",
        args_model=ClickArgs,
        handler=lambda args, ctx: ToolResult(ok=True),
        output_kind="action",
    )

    async def run():
        runner = PipelineRunner(
            DummySettings(),
            RepairModel(),
            FakeRegistry(spec),
            None,
            EventSink(),
        )
        pipe = Pipe(
            run_id="run_visible_click",
            request=(
                "On the left side of my screen, I have VS Code open. "
                "There is going to be one named Jarvis. I just want you to click on that."
            ),
        )
        pipe.normalized = pipe.request
        await runner._plan(pipe, RepairModel())
        return pipe

    pipe = asyncio.run(run())

    assert pipe.tasks["t1"].tool == "ui.click"
    assert pipe.tasks["t1"].args == {"target": "Jarvis"}
    assert "win.focus" not in [task.tool for task in pipe.tasks.values()]


def test_visible_click_pronoun_uses_recent_described_tab():
    spec = ToolSpec(
        name="ui.click",
        namespace="ui",
        description="Click a visible screen target.",
        args_model=ClickArgs,
        handler=lambda args, ctx: ToolResult(ok=True),
        output_kind="action",
    )

    async def run():
        runner = PipelineRunner(DummySettings(), RepairModel(), FakeRegistry(spec), None, EventSink())
        pipe = Pipe(
            run_id="run_youtube_tab",
            request=(
                "On the right side of my screen I have two tabs open my browser. "
                "The first one is Jarvis. The second one is just a simple YouTube. "
                "I want you to click on that tab so that it opens."
            ),
        )
        pipe.normalized = pipe.request
        await runner._plan(pipe, RepairModel())
        return pipe

    pipe = asyncio.run(run())

    assert pipe.tasks["t1"].tool == "ui.click"
    assert pipe.tasks["t1"].args == {"target": "YouTube tab"}


def test_visible_open_second_youtube_tab_forces_ui_click_plan():
    spec = ToolSpec(
        name="ui.click",
        namespace="ui",
        description="Click a visible screen target.",
        args_model=ClickArgs,
        handler=lambda args, ctx: ToolResult(ok=True),
        output_kind="action",
    )

    async def run():
        runner = PipelineRunner(DummySettings(), RepairModel(), FakeRegistry(spec), None, EventSink())
        pipe = Pipe(
            run_id="run_second_youtube_tab",
            request="Open the YouTube tab for me, the second YouTube tab.",
        )
        pipe.normalized = pipe.request
        await runner._plan(pipe, RepairModel())
        return pipe

    pipe = asyncio.run(run())

    assert pipe.tasks["t1"].tool == "ui.click"
    assert pipe.tasks["t1"].args == {"target": "second YouTube tab"}
    assert "win.focus" not in [task.tool for task in pipe.tasks.values()]


def test_visible_click_preserves_nearby_landmark():
    spec = ToolSpec(
        name="ui.click",
        namespace="ui",
        description="Click a visible screen target.",
        args_model=ClickArgs,
        handler=lambda args, ctx: ToolResult(ok=True),
        output_kind="action",
    )

    async def run():
        runner = PipelineRunner(DummySettings(), RepairModel(), FakeRegistry(spec), None, EventSink())
        pipe = Pipe(
            run_id="run_youtube_landmark",
            request="Click the second YouTube tab which shows 1785 right before it.",
        )
        pipe.normalized = pipe.request
        await runner._plan(pipe, RepairModel())
        return pipe

    pipe = asyncio.run(run())

    assert pipe.tasks["t1"].tool == "ui.click"
    assert pipe.tasks["t1"].args == {"target": "second YouTube tab with 1785 right before it"}


def test_visual_correction_retries_recent_click_target_from_history():
    spec = ToolSpec(
        name="ui.click",
        namespace="ui",
        description="Click a visible screen target.",
        args_model=ClickArgs,
        handler=lambda args, ctx: ToolResult(ok=True),
        output_kind="action",
    )

    async def run():
        runner = PipelineRunner(DummySettings(), RepairModel(), FakeRegistry(spec), None, EventSink())
        pipe = Pipe(
            run_id="run_youtube_retry",
            request="You keep on missing that YouTube tab you're not clicking on it.",
        )
        pipe.normalized = pipe.request
        pipe.history_context = (
            "Earlier in this conversation:\n"
            "- User asked: Click the second YouTube tab which shows 1785 right before it.\n"
            "- You answered: Done, I clicked the second YouTube tab."
        )
        await runner._plan(pipe, RepairModel())
        return pipe

    pipe = asyncio.run(run())

    assert pipe.tasks["t1"].tool == "ui.click"
    assert pipe.tasks["t1"].args == {"target": "second YouTube tab with 1785 right before it"}


def test_visual_correction_classification_forces_tools():
    spec = ToolSpec(
        name="ui.click",
        namespace="ui",
        description="Click a visible screen target.",
        args_model=ClickArgs,
        handler=lambda args, ctx: ToolResult(ok=True),
        output_kind="action",
    )

    async def run():
        runner = PipelineRunner(ClassifySettings(), NoToolsModel(), ClassifyRegistry(spec), None, EventSink())
        pipe = Pipe(
            run_id="run_youtube_classify_retry",
            request="You keep on missing that YouTube tab you're not clicking on it.",
        )
        pipe.normalized = pipe.request
        pipe.history_context = "- User asked: Click the second YouTube tab."
        await runner._classify(pipe, NoToolsModel())
        return pipe

    pipe = asyncio.run(run())

    assert pipe.level == "multistep"
    assert pipe.grounded


def test_visible_click_compose_returns_human_action_sentence():
    runner = PipelineRunner(DummySettings(), RepairModel(), FakeRegistry(None), None, EventSink())
    pipe = Pipe(
        run_id="run_action_answer",
        request="Click on the YouTube tab so that it opens.",
        normalized="Click on the YouTube tab so that it opens.",
    )
    pipe.results["t1"] = Result(
        ok=True,
        kind="action",
        source="ui.click",
        data={"clicked": "YouTube tab", "x": 370, "y": 20, "method": "vision"},
    )
    task = Task(id="compose", kind="compose", intent="compose the answer", depends_on=["t1"])

    result = asyncio.run(runner._exec_compose(task, pipe))

    assert result.ok
    assert result.text == "Done, I clicked the YouTube tab and opened it for you."
    assert "ui.click" not in result.text


def test_ui_click_uses_whole_screen_vision_locator_only(monkeypatch):
    from jarvis.tools import ui

    clicked = []

    def fake_locate(settings, target):
        assert target == "second YouTube tab"
        return (123, 45), "fake-grounding-model"

    monkeypatch.setattr(ui, "_vision_locate", fake_locate)
    monkeypatch.setattr(ui, "_click_xy", lambda x, y: clicked.append((x, y)))

    result = ui.ui_click(
        ui.TargetArgs(target="second YouTube tab"),
        SimpleNamespace(settings=object()),
    )

    assert result.ok
    assert result.data["method"] == "vision"
    assert result.data["model"] == "fake-grounding-model"
    assert clicked == [(123, 45)]


def test_ui_click_includes_vision_confidence_metadata(monkeypatch):
    from jarvis.tools import ui

    clicked = []

    monkeypatch.setattr(
        ui,
        "_vision_locate",
        lambda settings, target: (
            (321, 15),
            {
                "model": "fake-grounding-model",
                "confidence": 0.82,
                "verification_confidence": 0.91,
                "description": "tab header labeled YouTube",
            },
        ),
    )
    monkeypatch.setattr(ui, "_click_xy", lambda x, y: clicked.append((x, y)))

    result = ui.ui_click(ui.TargetArgs(target="YouTube tab"), SimpleNamespace(settings=object()))

    assert result.ok
    assert result.data["confidence"] == 0.82
    assert result.data["verification_confidence"] == 0.91
    assert "tab header" in result.data["description"]
    assert clicked == [(321, 15)]


def test_screen_read_text_uses_vision_model(monkeypatch):
    from jarvis.tools import screen, vision

    calls = []

    def fake_describe(settings, question):
        calls.append(question)
        return {"ok": True, "data": {"answer": "Visible text from the vision model."}}

    monkeypatch.setattr(vision, "describe", fake_describe)

    result = screen.screen_read_text(screen.ReadTextArgs(), SimpleNamespace(settings=object()))

    assert result.ok
    assert result.data == {"text": "Visible text from the vision model."}
    assert calls
    assert "visible text" in calls[0].lower()
