"""Tool contract, registry, and the @tool decorator (PLANv3 / PLANv2 §7).

Every capability is a typed Tool: a Pydantic args model (which becomes the JSON
schema sent to the model and validates the model's output), a handler, a risk
class, and a timeout. Tools self-register via @tool; the app builds a Registry
from the registered globals and uses it for schema export and subsetting.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field

from jarvis.model.client import ToolSchema

Risk = Literal["read", "write", "destructive", "system"]


class Artifact(BaseModel):
    id: str | None = None
    path: str
    type: str = "file"
    mime_type: str | None = None
    size_bytes: int | None = None


class ToolError(BaseModel):
    code: str
    message: str
    category: Literal[
        "validation", "timeout", "not_found", "permission",
        "execution", "cancelled", "dependency_missing", "tripwire", "unknown",
    ] = "unknown"
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error: ToolError | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None
    duration_ms: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    rollback_token: str | None = None
    redact_data: bool = False   # keep this tool's `data` out of the audit/tool-call log (e.g. screen text)


Handler = Callable[[BaseModel, "ToolContext"], ToolResult]


OutputKind = Literal["table", "list", "ranking", "scalar", "prose", "action"]


@dataclass
class ToolSpec:
    name: str
    namespace: str
    description: str
    args_model: type[BaseModel]
    handler: Handler
    risk: Risk = "read"
    timeout_s: int = 120
    cancellable: bool = False
    always_on: bool = False
    confirm: Literal["auto", "always", "never"] = "auto"  # auto = decide from risk + autonomy.confirm_level
    # --- PLANv4 capability metadata (drives selection, feasibility, validation, render) ---
    capabilities: list[str] = field(default_factory=list)   # e.g. ["files.enumerate", "disk.measure"]
    output_kind: OutputKind = "prose"                        # how a successful result should be rendered
    output_schema: dict[str, Any] = field(default_factory=dict)  # declared shape of `data` on success
    examples: list[dict] = field(default_factory=list)       # {"request":..., "args":{...}} few-shots for arg-binding

    def schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=self.args_model.model_json_schema(),
        )


class Tool:  # thin alias kept for typing/readability
    pass


# Global registry populated by the decorator at import time.
_GLOBAL_SPECS: dict[str, ToolSpec] = {}


def tool(
    *,
    name: str,
    risk: Risk = "read",
    timeout_s: int = 120,
    cancellable: bool = False,
    always_on: bool = False,
    confirm: str = "auto",
    capabilities: list[str] | None = None,
    output_kind: str = "prose",
    output_schema: dict | None = None,
    examples: list[dict] | None = None,
) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        # eval_str resolves string annotations produced by `from __future__ import
        # annotations` (PEP 563) so the first-arg type is a class, not a str.
        try:
            sig = inspect.signature(fn, eval_str=True)
        except Exception:
            sig = inspect.signature(fn)
        params = list(sig.parameters.values())
        if not params:
            raise TypeError(f"tool {name} handler must accept (args, ctx)")
        args_model = params[0].annotation
        if isinstance(args_model, str):
            # Fallback: resolve via the function's module globals.
            args_model = fn.__globals__.get(args_model, args_model)
        if not (isinstance(args_model, type) and issubclass(args_model, BaseModel)):
            raise TypeError(f"tool {name}: first arg must be annotated with a BaseModel")
        spec = ToolSpec(
            name=name,
            namespace=name.split(".")[0],
            description=(fn.__doc__ or name).strip(),
            args_model=args_model,
            handler=fn,
            risk=risk,
            timeout_s=timeout_s,
            cancellable=cancellable,
            always_on=always_on,
            confirm=confirm,
            capabilities=list(capabilities or []),
            output_kind=output_kind,
            output_schema=dict(output_schema or {}),
            examples=list(examples or []),
        )
        _GLOBAL_SPECS[name] = spec
        return fn

    return deco


class Registry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def load_globals(self) -> None:
        self._specs.update(_GLOBAL_SPECS)

    def register(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def all(self) -> list[ToolSpec]:
        return list(self._specs.values())

    def names(self) -> list[str]:
        return list(self._specs)

    def schemas(self, names: list[str] | None = None) -> list[ToolSchema]:
        specs = [self._specs[n] for n in names if n in self._specs] if names else self.all()
        return [s.schema() for s in specs]

    def capability_index(self) -> "CapabilityIndex":
        """Build a fresh capability index over the currently-registered tools."""
        return CapabilityIndex(self)

    def select_by_capability(self, caps: list[str], max_tools: int = 14) -> list[str]:
        """PLANv4 selection: always-on core + every tool providing a requested capability."""
        idx = self.capability_index()
        chosen: list[str] = [s.name for s in self.all() if s.always_on]
        for c in caps:
            for n in idx.tools_for(c):
                if n not in chosen:
                    chosen.append(n)
        return chosen[:max_tools] if max_tools else chosen

    # -- subsetting (PLANv3 / PLANv2 §6.6, 8.5) ---------------------------- #
    def select_for(self, goal: str, max_tools: int = 14) -> list[str]:
        """Pick a relevant subset: always-on core + namespace/keyword matches."""
        goal_l = goal.lower()
        chosen: list[str] = [s.name for s in self.all() if s.always_on]
        scored: list[tuple[float, str]] = []
        for s in self.all():
            if s.always_on:
                continue
            score = 0.0
            if s.namespace in goal_l:
                score += 2.0
            for word in s.description.lower().split():
                if len(word) > 3 and word in goal_l:
                    score += 0.5
            # heuristic keyword -> namespace hints
            hints = {
                "fs": ["file", "folder", "directory", "read", "write", "organize", "delete"],
                "shell": ["run", "command", "powershell", "script", "install"],
                "vault": ["remember", "memory", "note", "recall", "project", "skill"],
                "web": ["search", "google", "look up", "internet", "web", "news", "url", "website", "online"],
                "screen": ["screen", "see", "look", "display", "monitor", "read screen"],
                "input": ["click", "type", "press", "tap", "mouse", "keyboard", "scroll", "drag"],
                "win": ["launch", "app", "application"],
                "ui": ["click", "button", "element", "select", "field", "menu", "icon", "tab", "checkbox", "link", "press the"],
                "sys": ["admin", "administrator", "elevate", "elevated", "install", "service", "shutdown", "restart", "sleep", "lock", "registry",
                        "disk", "space", "storage", "capacity", "drive", "free space", "taking up", "how big", "size of my"],
            }
            for kw in hints.get(s.namespace, []):
                if kw in goal_l:
                    score += 1.0
            if score > 0:
                scored.append((score, s.name))
        scored.sort(reverse=True)
        for _, n in scored:
            if n not in chosen:
                chosen.append(n)
            if len(chosen) >= max_tools:
                break
        return chosen


class CapabilityIndex:
    """Maps capability tags -> tools, for deterministic selection, feasibility, and a compact
    catalog string the planner/classifier can read. Built from a Registry (PLANv4 §6)."""

    def __init__(self, registry: "Registry") -> None:
        self.registry = registry
        self._cap_to_tools: dict[str, list[str]] = {}
        for s in registry.all():
            for c in (s.capabilities or []):
                self._cap_to_tools.setdefault(c, []).append(s.name)

    def capabilities(self) -> list[str]:
        return sorted(self._cap_to_tools)

    def tools_for(self, capability: str) -> list[str]:
        return list(self._cap_to_tools.get(capability, []))

    def covers(self, caps: list[str]) -> bool:
        """True iff every requested capability is provided by at least one tool."""
        return all(self._cap_to_tools.get(c) for c in caps)

    def missing(self, caps: list[str]) -> list[str]:
        """The requested capabilities that no tool provides (feasibility gate)."""
        return [c for c in caps if not self._cap_to_tools.get(c)]

    def catalog(self, names: list[str] | None = None) -> str:
        """Compact one-line-per-tool catalog for prompts: name, summary, caps, output kind."""
        specs = ([self.registry.get(n) for n in names] if names else self.registry.all())
        lines: list[str] = []
        for s in specs:
            if not s:
                continue
            summary = (s.description or s.name).strip().splitlines()[0][:90]
            caps = ", ".join(s.capabilities) if s.capabilities else "-"
            lines.append(f"- {s.name} [{caps}] -> {s.output_kind}: {summary}")
        return "\n".join(lines)


@dataclass
class ToolContext:
    run_id: str
    working_directory: Optional[str]
    settings: Any                       # jarvis.config.Settings
    rollback: Any = None                # RollbackManager
    policy: Any = None                  # Policy
    vault: Any = None                   # Vault
    retriever: Any = None               # Retriever
    store: Any = None                   # Store
    registry: Any = None                # Registry (for tools.list/describe)
    project: Optional[str] = None       # active project scope, if any
    logger: Any = None
    processes: list = field(default_factory=list)   # live Popen handles for cancel
    _cancelled: Callable[[], bool] = field(default=lambda: False)

    def cancelled(self) -> bool:
        try:
            return bool(self._cancelled())
        except Exception:
            return False
