"""Meta tool: tool introspection."""
from __future__ import annotations

from pydantic import BaseModel, Field

from jarvis.tools.base import ToolContext, ToolResult, tool


class ToolsListArgs(BaseModel):
    namespace: str | None = Field(None, description="Optional namespace filter, e.g. 'fs'.")


@tool(name="tools.list", risk="read", always_on=True,
      capabilities=["meta.tools"], output_kind="list")
def tools_list(args: ToolsListArgs, ctx: ToolContext) -> ToolResult:
    """List available tools, optionally filtered by namespace."""
    reg = ctx.registry
    names: list[str] = []
    if reg is not None:
        for s in reg.all():
            if args.namespace and s.namespace != args.namespace:
                continue
            names.append(s.name)
    return ToolResult(ok=True, data={"tools": names})
