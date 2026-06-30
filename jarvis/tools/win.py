"""Application launch tool.

Window enumeration, focus, move, resize, and close shortcuts are intentionally not
exposed. Screen/window state should be understood through the vision model, and
visible targets should be acted on through ui.click.
"""
from __future__ import annotations

import subprocess
import sys

from pydantic import BaseModel, Field

from jarvis.tools.base import ToolContext, ToolError, ToolResult, tool

_IS_WINDOWS = sys.platform.startswith("win")


class LaunchArgs(BaseModel):
    app: str = Field(..., description="Program to start: a Start-menu name, full path, URL, or URI.")
    args: str = Field("", description="Optional command-line arguments.")


@tool(
    name="win.launch",
    risk="write",
    timeout_s=30,
    capabilities=["app.launch"],
    output_kind="action",
    examples=[{"request": "open Notepad", "args": {"app": "notepad"}}],
)
def win_launch(args: LaunchArgs, ctx: ToolContext) -> ToolResult:
    """Launch an application, file, or URL without inspecting monitor/window state."""
    try:
        if _IS_WINDOWS:
            cmd = f'start "" "{args.app}" {args.args}'.strip()
            subprocess.Popen(["powershell", "-NoProfile", "-Command", cmd])
        else:
            subprocess.Popen([args.app] + (args.args.split() if args.args else []))
        return ToolResult(ok=True, data={"launched": args.app})
    except Exception as e:
        return ToolResult(ok=False, error=ToolError(code="launch_failed", category="execution", message=str(e)))
