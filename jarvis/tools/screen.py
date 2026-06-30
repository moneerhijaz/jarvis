"""Vision-only screen tools.

All screen/monitor understanding goes through the local vision model. This module
does not expose raw screenshot capture, display enumeration, OCR, window metadata,
or any other monitor shortcut to the agent.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from jarvis.tools.base import ToolContext, ToolError, ToolResult, tool


class ReadTextArgs(BaseModel):
    question: str | None = Field(
        None,
        description="Optional text-reading question; default = read the visible screen text.",
    )


@tool(
    name="screen.read_text",
    risk="read",
    timeout_s=120,
    capabilities=["screen.read", "screen.understand", "vision.screen"],
    output_kind="prose",
    output_schema={"text": "str"},
    examples=[{"request": "read the text on my screen", "args": {}}],
)
def screen_read_text(args: ReadTextArgs, ctx: ToolContext) -> ToolResult:
    """Read visible screen text using the vision model, not OCR."""
    from jarvis.tools import vision

    question = args.question or (
        "Read the visible text on the whole screen. Return the important text in a concise, "
        "organized way. If text is unclear or not visible, say that clearly."
    )
    res = vision.describe(ctx.settings, question)
    if not res.get("ok"):
        return ToolResult(
            ok=False,
            error=ToolError(
                code="vision_failed",
                category="execution",
                message=str(res.get("reason") or "vision could not read the screen"),
            ),
        )
    answer = str((res.get("data") or {}).get("answer") or "").strip()
    return ToolResult(ok=True, redact_data=True, data={"text": answer})


class LookArgs(BaseModel):
    question: str | None = Field(None, description="Optional question about the screen; default = describe it.")


@tool(
    name="screen.look",
    risk="read",
    timeout_s=120,
    capabilities=["screen.describe", "screen.understand", "vision.screen"],
    output_kind="prose",
    output_schema={"answer": "str"},
    examples=[
        {"request": "what's on my screen", "args": {}},
        {"request": "what app is open", "args": {"question": "Which application is in the foreground?"}},
    ],
)
def screen_look(args: LookArgs, ctx: ToolContext) -> ToolResult:
    """Look at the whole screen with the vision model and describe it."""
    from jarvis.tools import vision

    res = vision.describe(ctx.settings, args.question)
    if not res.get("ok"):
        return ToolResult(
            ok=False,
            error=ToolError(
                code="vision_failed",
                category="execution",
                message=str(res.get("reason") or "vision could not read the screen"),
            ),
        )
    return ToolResult(ok=True, redact_data=True, data=res["data"])
