from jarvis.tools.base import (
    Artifact,
    Registry,
    Tool,
    ToolContext,
    ToolError,
    ToolResult,
    ToolSpec,
    tool,
)
from jarvis.tools.executor import ToolExecutor


def build_default_registry() -> Registry:
    """Import the tool modules (registering their tools) and return a Registry."""
    from jarvis.tools import fs, meta, shell, vault_tools  # noqa: F401  (import side effects)
    # Agentic computer-control + web tools (AGENTIC_PLAN). Imports are side-effect only;
    # the heavy OS/web libraries inside are lazy, so importing these never fails the boot.
    from jarvis.tools import screen, input as _input, win, sys as _sys, web, ui  # noqa: F401

    reg = Registry()
    reg.load_globals()
    return reg


__all__ = [
    "Artifact",
    "Registry",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolResult",
    "ToolSpec",
    "tool",
    "ToolExecutor",
    "build_default_registry",
]
