"""Deterministic model client for tests and offline development.

Drive the agent loop with a scripted sequence of responses. Each scripted step is
either a tool call or a final answer. Supports a callable form so a test can react
to the running messages (e.g. emit a tool call only on the first turn).
"""
from __future__ import annotations

from typing import Any, Callable, Iterator

from jarvis.model.client import (
    Message,
    ModelHealth,
    ModelInfo,
    ModelResponse,
    ToolCall,
    ToolSchema,
    Usage,
)

Scripted = ModelResponse
Scripter = Callable[[list[Message], list[ToolSchema] | None], ModelResponse]


def tool_step(name: str, arguments: dict[str, Any], call_id: str = "call_x") -> ModelResponse:
    return ModelResponse(
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        finish_reason="tool_calls",
        provider="fake",
        model="fake",
    )


def final_step(content: str) -> ModelResponse:
    return ModelResponse(content=content, finish_reason="stop", provider="fake", model="fake")


class FakeModelClient:
    provider = "fake"

    def __init__(self, script: list[ModelResponse] | Scripter | None = None) -> None:
        self._script = script or [final_step("ok")]
        self._i = 0
        self.calls: list[dict[str, Any]] = []  # record of chat() invocations

    def health(self) -> ModelHealth:
        return ModelHealth(provider=self.provider, base_url="memory://fake", reachable=True, models=["fake"], default_model="fake")

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(id="fake")]

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        tool_choice: str = "auto",
    ) -> ModelResponse:
        self.calls.append({"messages": messages, "tools": [t.name for t in (tools or [])]})
        if callable(self._script):
            resp = self._script(messages, tools)
        else:
            if self._i >= len(self._script):
                resp = final_step("done")
            else:
                resp = self._script[self._i]
                self._i += 1
        if resp.usage is None:
            resp = resp.model_copy(update={"usage": Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15)})
        return resp

    def stream_chat(self, *args: Any, **kwargs: Any) -> Iterator[str]:
        resp = self.chat(*args, **kwargs)
        if resp.content:
            yield resp.content

    async def astream_chat(self, messages, tools=None, **kwargs):
        resp = self.chat(messages, tools)
        if resp.tool_calls:
            yield {"type": "final", "tool_calls": resp.tool_calls}
            return
        text = resp.content or ""
        n = max(1, len(text) // 3)  # a few chunks that re-concatenate to the exact text
        for i in range(0, len(text), n):
            yield {"type": "content", "text": text[i:i + n]}
        yield {"type": "final", "tool_calls": []}

    def embed(self, texts: list[str]) -> list[list[float]]:
        # Cheap deterministic embedding: char-bucket histogram (no deps).
        out = []
        for t in texts:
            vec = [0.0] * 32
            for ch in t.lower():
                vec[ord(ch) % 32] += 1.0
            norm = sum(v * v for v in vec) ** 0.5 or 1.0
            out.append([v / norm for v in vec])
        return out
