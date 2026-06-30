"""LM Studio adapter (OpenAI-compatible REST, via httpx).

Targets ``/v1/models``, ``/v1/chat/completions``, ``/v1/embeddings`` exactly as
the OpenAI client would, so the same code points at Ollama/llama.cpp by changing
base_url. Tool calls are normalized into ``ToolCall`` objects.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Iterator

import httpx

from jarvis.model.client import (
    Message,
    ModelHealth,
    ModelInfo,
    ModelResponse,
    ToolCall,
    ToolSchema,
    Usage,
)
from jarvis.events import now_iso


_TOOLCALL_BLOCK = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
_FUNC_BLOCK = re.compile(r"<function\s*=\s*([^>\s]+)\s*>(.*?)</function>", re.S)
_PARAM_BLOCK = re.compile(r"<parameter\s*=\s*([^>\s]+)\s*>(.*?)</parameter>", re.S)


def _norm_tool_name(n: str) -> str:
    """Models sometimes write tool names with an underscore (shell_run) instead of the dot
    form (shell.run). Map the first underscore to a dot when there's no dot already."""
    n = (n or "").strip()
    if "." not in n and "_" in n:
        n = n.replace("_", ".", 1)
    return n


def parse_inline_tool_calls(text: str) -> list[ToolCall]:
    """Fallback for models that emit tool calls as TEXT instead of native function calls
    (Qwen3/Hermes do this when native calling doesn't fire). Handles both
    `<tool_call>{"name":..,"arguments":{..}}</tool_call>` and
    `<function=name><parameter=key>value</parameter>...</function>`."""
    calls: list[ToolCall] = []
    for i, m in enumerate(_TOOLCALL_BLOCK.finditer(text or "")):
        try:
            obj = json.loads(m.group(1))
        except Exception:
            continue
        name = obj.get("name") or obj.get("function") or ""
        args = obj.get("arguments")
        if args is None:
            args = obj.get("parameters") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        if name:
            calls.append(ToolCall(id=f"call_inline_{i}", name=_norm_tool_name(name),
                                  arguments=args if isinstance(args, dict) else {}))
    base = len(calls)
    for j, m in enumerate(_FUNC_BLOCK.finditer(text or "")):
        name = m.group(1)
        args = {k.strip(): v.strip() for k, v in _PARAM_BLOCK.findall(m.group(2) or "")}
        if name:
            calls.append(ToolCall(id=f"call_inline_{base + j}", name=_norm_tool_name(name), arguments=args))
    return calls


class LMStudioClient:
    provider = "lmstudio"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:1234/v1",
        api_key: str = "lm-studio",
        model: str = "auto",
        timeout_s: int = 120,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._model = model
        self.timeout_s = timeout_s

    # -- helpers ----------------------------------------------------------- #
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _resolve_model(self) -> str:
        if self._model and self._model != "auto":
            return self._model
        models = self.list_models()
        if not models:
            raise RuntimeError("LM Studio reports no loaded models")
        return models[0].id

    # -- API --------------------------------------------------------------- #
    def health(self) -> ModelHealth:
        try:
            models = self.list_models()
            return ModelHealth(
                provider=self.provider,
                base_url=self.base_url,
                reachable=True,
                models=[m.id for m in models],
                default_model=models[0].id if models else None,
                checked_at=now_iso(),
            )
        except Exception as e:
            return ModelHealth(
                provider=self.provider,
                base_url=self.base_url,
                reachable=False,
                error=str(e),
                checked_at=now_iso(),
            )

    def list_models(self) -> list[ModelInfo]:
        with httpx.Client(timeout=self.timeout_s) as c:
            r = c.get(f"{self.base_url}/models", headers=self._headers())
            r.raise_for_status()
            data = r.json().get("data", [])
        return [ModelInfo(id=m["id"], owned_by=m.get("owned_by")) for m in data]

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        tool_choice: str = "auto",
    ) -> ModelResponse:
        model = self._resolve_model()
        body: dict[str, Any] = {
            "model": model,
            "messages": [self._encode_message(m) for m in messages],
            "temperature": temperature,
            # Qwen3 (incl. VL) defaults to "thinking", which puts output in reasoning_content
            # and leaves content empty -> we'd see no answer and no tool call. Turn it off.
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = [t.as_openai() for t in tools]
            body["tool_choice"] = tool_choice

        t0 = time.time()
        with httpx.Client(timeout=self.timeout_s) as c:
            r = c.post(f"{self.base_url}/chat/completions", headers=self._headers(), json=body)
            r.raise_for_status()
            data = r.json()
        latency_ms = int((time.time() - t0) * 1000)

        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        tool_calls = self._parse_tool_calls(msg)
        content = msg.get("content")
        if not tool_calls and (content or "").strip():       # tool calls emitted as text?
            inline = parse_inline_tool_calls(content)
            if inline:
                tool_calls, content = inline, None
        if not (content or "").strip() and not tool_calls:   # thinking-only turn: salvage the reasoning
            content = msg.get("reasoning_content") or msg.get("reasoning") or content
        usage_raw = data.get("usage") or {}
        return ModelResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason"),
            usage=Usage(
                prompt_tokens=usage_raw.get("prompt_tokens"),
                completion_tokens=usage_raw.get("completion_tokens"),
                total_tokens=usage_raw.get("total_tokens"),
            ),
            provider=self.provider,
            model=model,
            latency_ms=latency_ms,
            raw_metadata={"finish_reason": choice.get("finish_reason")},
        )

    def stream_chat(self, *args: Any, **kwargs: Any) -> Iterator[str]:
        # Non-streaming fallback: yield the whole content once.
        resp = self.chat(*args, **kwargs)
        if resp.content:
            yield resp.content

    async def astream_chat(
        self,
        messages: list[Message],
        tools: list[ToolSchema] | None = None,
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        tool_choice: str = "auto",
    ):
        """Async generator of streaming events:
        {"type":"content","text":...}  -> a token chunk of a text answer
        {"type":"final","tool_calls":[ToolCall...]}  -> end, with any tool calls
        """
        model = self._resolve_model()
        body: dict[str, Any] = {
            "model": model,
            "messages": [self._encode_message(m) for m in messages],
            "temperature": temperature,
            "stream": True,
            # Disable Qwen3 thinking so the answer streams as content (not reasoning_content),
            # which otherwise leaves the turn empty -> "no content or tool call".
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if max_tokens:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = [t.as_openai() for t in tools]
            body["tool_choice"] = tool_choice

        tool_acc: dict[int, dict[str, Any]] = {}
        saw_content = False
        content_buf = ""
        reasoning_acc = ""
        async with httpx.AsyncClient(timeout=self.timeout_s) as c:
            async with c.stream("POST", f"{self.base_url}/chat/completions", headers=self._headers(), json=body) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta", {})
                    if delta.get("content"):
                        saw_content = True
                        content_buf += delta["content"]
                        yield {"type": "content", "text": delta["content"]}
                    elif delta.get("reasoning_content") or delta.get("reasoning"):
                        reasoning_acc += delta.get("reasoning_content") or delta.get("reasoning") or ""
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        acc = tool_acc.setdefault(idx, {"id": tc.get("id"), "name": "", "args": ""})
                        if tc.get("id"):
                            acc["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            acc["name"] = fn["name"]
                        if fn.get("arguments"):
                            acc["args"] += fn["arguments"]
        calls: list[ToolCall] = []
        for idx in sorted(tool_acc):
            acc = tool_acc[idx]
            try:
                args = json.loads(acc["args"]) if acc["args"].strip() else {}
            except json.JSONDecodeError:
                args = {"_raw": acc["args"]}
            calls.append(ToolCall(id=acc["id"] or f"call_{idx}", name=acc["name"], arguments=args))
        # Tool calls emitted as TEXT (Qwen3/Hermes fallback): parse them out of the streamed
        # content and tell the loop to DROP that content (it was markup, not an answer).
        if not calls and content_buf.strip():
            inline = parse_inline_tool_calls(content_buf)
            if inline:
                yield {"type": "final", "tool_calls": inline, "drop_content": True}
                return
        # Thinking-only turn (enable_thinking ignored by the server): surface the reasoning as
        # a THOUGHT, NOT as the answer — otherwise the raw chain-of-thought gets spoken to the
        # user. The loop will re-nudge the model to produce a real final.answer.
        if not saw_content and not calls and reasoning_acc.strip():
            import re as _re
            txt = _re.sub(r"(?is)<think>.*?</think>", "", reasoning_acc).strip() or reasoning_acc.strip()
            yield {"type": "reasoning", "text": txt}
        yield {"type": "final", "tool_calls": calls}

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._resolve_model()
        with httpx.Client(timeout=self.timeout_s) as c:
            r = c.post(
                f"{self.base_url}/embeddings",
                headers=self._headers(),
                json={"model": model, "input": texts},
            )
            r.raise_for_status()
            data = r.json().get("data", [])
        return [item["embedding"] for item in data]

    # -- encoding ---------------------------------------------------------- #
    @staticmethod
    def _encode_message(m: Message) -> dict[str, Any]:
        out: dict[str, Any] = {"role": m.role}
        if getattr(m, "images", None):
            # OpenAI-style multimodal content: text block + image_url blocks
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for url in m.images:
                blocks.append({"type": "image_url", "image_url": {"url": url}})
            out["content"] = blocks
        elif m.content is not None:
            out["content"] = m.content
        if m.tool_calls:
            out["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                }
                for tc in m.tool_calls
            ]
        if m.tool_call_id:
            out["tool_call_id"] = m.tool_call_id
        if m.name:
            out["name"] = m.name
        return out

    @staticmethod
    def _parse_tool_calls(msg: dict[str, Any]) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    args = {"_raw": args}
            calls.append(ToolCall(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""), arguments=args or {}))
        return calls
