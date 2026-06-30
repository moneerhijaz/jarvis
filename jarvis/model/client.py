"""Model gateway contracts.

The agent talks only to a ``ModelClient``. Implementations (LM Studio, a fake for
tests, later Ollama/llama.cpp) hide provider quirks and normalize tool calls into
the same shape, so swapping runtimes is a config change, not a code change.
"""
from __future__ import annotations

from typing import Any, Iterator, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class Message(BaseModel):
    role: str  # system | user | assistant | tool
    content: str | None = None
    # For assistant tool-call turns / tool result turns:
    tool_calls: list["ToolCall"] = Field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    images: list[str] = Field(default_factory=list)   # data: URLs for multimodal (vision) messages


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolSchema(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema

    def as_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class Usage(BaseModel):
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class ModelResponse(BaseModel):
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    finish_reason: str | None = None
    usage: Usage | None = None
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class ModelInfo(BaseModel):
    id: str
    owned_by: str | None = None


class ModelHealth(BaseModel):
    provider: str
    base_url: str
    reachable: bool
    models: list[str] = Field(default_factory=list)
    default_model: str | None = None
    error: str | None = None
    checked_at: str | None = None


@runtime_checkable
class ModelClient(Protocol):
    provider: str

    def health(self) -> ModelHealth: ...
    def list_models(self) -> list[ModelInfo]: ...
    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        tool_choice: str = "auto",
    ) -> ModelResponse: ...
    def stream_chat(self, *args: Any, **kwargs: Any) -> Iterator[str]: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...


Message.model_rebuild()
