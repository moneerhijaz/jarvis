"""Vision-only UI targeting.

Visible target actions must use the same path every time:
  1. Capture the whole screen through jarvis.tools.vision.locate.
  2. Ask the local vision/grounding model for the target pixel.
  3. Click that pixel.

This module intentionally does not inspect alternate OS/app data sources when finding
targets. Those paths were accurate sometimes, but they made the agent choose brittle
non-visual plans for requests like "open the second YouTube tab". If the user asks
JARVIS to click/open a visible thing, the screenshot model is the source of truth.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from jarvis.tools.base import ToolContext, ToolError, ToolResult, tool

_TGT_KEYS = ("target", "description", "label", "text", "element", "query", "name", "element_name", "item")


def _vision_locate(settings, target: str):
    """Locate a click target via the shared whole-screen vision helper."""
    from jarvis.tools import vision

    return vision.locate(settings, target)


def _vision_info(info):
    if isinstance(info, dict):
        return dict(info)
    return {"model": info}


def _click_xy(x: int, y: int):
    import pyautogui

    pyautogui.FAILSAFE = True
    pyautogui.click(x, y)


class TargetArgs(BaseModel):
    target: str = Field(
        ...,
        description=(
            "Natural-language description of the visible screen target, e.g. "
            "'the second YouTube tab' or 'the Jarvis folder'."
        ),
    )
    model_config = {"populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data):
        """Accept the common key names small models use for the target."""
        if not isinstance(data, dict):
            return data
        d = dict(data)
        if "target" not in d or not d.get("target"):
            for k in _TGT_KEYS:
                if k in d and isinstance(d[k], str) and d[k].strip():
                    d["target"] = d[k]
                    break
            else:
                for _, v in d.items():
                    if isinstance(v, str) and v.strip():
                        d["target"] = v
                        break
        return d


@tool(
    name="ui.find",
    risk="read",
    timeout_s=90,
    capabilities=["ui.locate", "vision.locate"],
    output_kind="scalar",
    output_schema={"x": "int", "y": "int", "method": "str", "target": "str"},
)
def ui_find(args: TargetArgs, ctx: ToolContext) -> ToolResult:
    """Locate a visible target using only a whole-screen screenshot and the vision model."""
    xy, info = _vision_locate(ctx.settings, args.target)
    if xy:
        vinf = _vision_info(info)
        return ToolResult(
            ok=True,
            data={"x": xy[0], "y": xy[1], "method": "vision", "target": args.target, **vinf},
        )
    return ToolResult(
        ok=False,
        error=ToolError(
            code="not_found",
            category="not_found",
            message=f"could not locate '{args.target}' from the whole-screen screenshot: {info}",
        ),
    )


@tool(
    name="ui.click",
    risk="write",
    timeout_s=90,
    capabilities=["ui.click", "input.click", "vision.locate"],
    output_kind="action",
    examples=[
        {"request": "open the second YouTube tab", "args": {"target": "second YouTube tab"}},
        {"request": "click the Jarvis folder", "args": {"target": "Jarvis folder"}},
    ],
)
def ui_click(args: TargetArgs, ctx: ToolContext) -> ToolResult:
    """Click a visible target using only whole-screen screenshot model grounding."""
    xy, info = _vision_locate(ctx.settings, args.target)
    if not xy:
        return ToolResult(
            ok=False,
            error=ToolError(
                code="not_found",
                category="not_found",
                message=f"could not locate '{args.target}' from the whole-screen screenshot: {info}",
            ),
        )
    try:
        _click_xy(*xy)
    except Exception as e:
        return ToolResult(ok=False, error=ToolError(code="click_failed", category="execution", message=str(e)))
    vinf = _vision_info(info)
    return ToolResult(
        ok=True,
        data={"clicked": args.target, "x": xy[0], "y": xy[1], "method": "vision", **vinf},
    )
