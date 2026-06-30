"""Mouse & keyboard tools (AGENTIC_PLAN §3.2).

Drive the real cursor/keyboard via pyautogui. Safety: the pyautogui corner FAILSAFE
is left ON (slam the mouse to a screen corner to abort), and moves use a small
duration so actions are visible. Coordinates should come from vision grounding. Risk is
'write' so these auto-run under "confirm risky only" — the brakes are the failsafe,
the kill switch (cancel kills the run), and the loop governors.
"""
from __future__ import annotations

import time

from pydantic import BaseModel, Field

from jarvis.tools.base import ToolContext, ToolError, ToolResult, tool

# Rate limit: cap mutating input actions so a runaway loop can't spray clicks/keys.
_RECENT: list[float] = []
_MAX_PER_SEC = 25


def _rate_limited() -> ToolResult | None:
    now = time.time()
    _RECENT[:] = [t for t in _RECENT if now - t < 1.0]
    if len(_RECENT) >= _MAX_PER_SEC:
        return ToolResult(ok=False, error=ToolError(
            code="rate_limited", category="execution",
            message=f"input rate limit ({_MAX_PER_SEC}/s) hit — slowing down"))
    _RECENT.append(now)
    return None


def _pg():
    import pyautogui  # type: ignore
    pyautogui.FAILSAFE = True       # mouse to a corner aborts
    pyautogui.PAUSE = 0.02
    return pyautogui


def _missing(e) -> ToolResult:
    return ToolResult(ok=False, error=ToolError(
        code="dependency_missing", category="dependency_missing",
        message=f"mouse/keyboard control needs 'pyautogui': {e}"))


def _clamp(x: int, y: int) -> tuple[int, int]:
    """Normalize coordinates without reading monitor geometry."""
    return int(x), int(y)


class MoveArgs(BaseModel):
    x: int = Field(..., description="Absolute screen X (pixels).")
    y: int = Field(..., description="Absolute screen Y (pixels).")
    duration: float = Field(0.15, description="Seconds to glide there.")


@tool(name="input.move", risk="write", timeout_s=20,
      capabilities=["input.move"], output_kind="action")
def input_move(args: MoveArgs, ctx: ToolContext) -> ToolResult:
    """Move the mouse cursor to absolute screen coordinates."""
    try:
        pg = _pg()
    except Exception as e:
        return _missing(e)
    _lim = _rate_limited()
    if _lim:
        return _lim
    x, y = _clamp(args.x, args.y)
    pg.moveTo(x, y, duration=max(0, min(2, args.duration)))
    return ToolResult(ok=True, data={"x": x, "y": y})


class ClickArgs(BaseModel):
    x: int | None = Field(None, description="X to click; omit to click the current position.")
    y: int | None = Field(None, description="Y to click; omit to click the current position.")
    button: str = Field("left", description="left | right | middle")
    clicks: int = Field(1, description="1 = single, 2 = double.")


@tool(name="input.click", risk="write", timeout_s=20,
      capabilities=["input.click"], output_kind="action")
def input_click(args: ClickArgs, ctx: ToolContext) -> ToolResult:
    """Click the mouse (optionally move to x,y first)."""
    try:
        pg = _pg()
    except Exception as e:
        return _missing(e)
    _lim = _rate_limited()
    if _lim:
        return _lim
    kw = {"button": args.button if args.button in ("left", "right", "middle") else "left",
          "clicks": max(1, min(3, args.clicks))}
    if args.x is not None and args.y is not None:
        cx, cy = _clamp(args.x, args.y)
        pg.click(cx, cy, **kw)
    else:
        cx, cy = pg.position()
        pg.click(**kw)
    return ToolResult(ok=True, data={"x": cx, "y": cy, "button": kw["button"], "clicks": kw["clicks"]})


class DragArgs(BaseModel):
    x1: int; y1: int; x2: int; y2: int
    button: str = Field("left", description="Mouse button to hold.")
    duration: float = Field(0.4, description="Seconds for the drag.")


@tool(name="input.drag", risk="write", timeout_s=20,
      capabilities=["input.drag"], output_kind="action")
def input_drag(args: DragArgs, ctx: ToolContext) -> ToolResult:
    """Press at (x1,y1), drag to (x2,y2), release."""
    try:
        pg = _pg()
    except Exception as e:
        return _missing(e)
    _lim = _rate_limited()
    if _lim:
        return _lim
    x1, y1 = _clamp(args.x1, args.y1)
    x2, y2 = _clamp(args.x2, args.y2)
    pg.moveTo(x1, y1, duration=0.1)
    pg.dragTo(x2, y2, duration=max(0.05, min(3, args.duration)),
              button=args.button if args.button in ("left", "right", "middle") else "left")
    return ToolResult(ok=True, data={"from": [x1, y1], "to": [x2, y2]})


class ScrollArgs(BaseModel):
    amount: int = Field(..., description="Lines to scroll; positive = up, negative = down.")
    x: int | None = None
    y: int | None = None


@tool(name="input.scroll", risk="write", timeout_s=20,
      capabilities=["input.scroll"], output_kind="action")
def input_scroll(args: ScrollArgs, ctx: ToolContext) -> ToolResult:
    """Scroll the wheel (optionally at a position)."""
    try:
        pg = _pg()
    except Exception as e:
        return _missing(e)
    _lim = _rate_limited()
    if _lim:
        return _lim
    if args.x is not None and args.y is not None:
        pg.scroll(args.amount, x=args.x, y=args.y)
    else:
        pg.scroll(args.amount)
    return ToolResult(ok=True, data={"amount": args.amount})


class TypeArgs(BaseModel):
    text: str = Field(..., description="Text to type at the current focus.")
    interval: float = Field(0.01, description="Seconds between keystrokes.")


@tool(name="input.type", risk="write", timeout_s=60,
      capabilities=["input.type", "keyboard.text"], output_kind="action")
def input_type(args: TypeArgs, ctx: ToolContext) -> ToolResult:
    """Type text into whatever currently has keyboard focus."""
    try:
        pg = _pg()
    except Exception as e:
        return _missing(e)
    _lim = _rate_limited()
    if _lim:
        return _lim
    pg.typewrite(args.text, interval=max(0, min(0.2, args.interval)))
    return ToolResult(ok=True, data={"typed_chars": len(args.text)})


class PressArgs(BaseModel):
    keys: str = Field(..., description="A key or hotkey combo, e.g. 'enter', 'esc', 'ctrl+s', 'alt+tab'.")


@tool(name="input.press", risk="write", timeout_s=20,
      capabilities=["input.press", "keyboard.shortcut"], output_kind="action",
      examples=[{"request": "close the current tab", "args": {"keys": "ctrl+w"}}])
def input_press(args: PressArgs, ctx: ToolContext) -> ToolResult:
    """Press a key or a hotkey combination (use '+' to combine, e.g. 'ctrl+shift+esc')."""
    try:
        pg = _pg()
    except Exception as e:
        return _missing(e)
    _lim = _rate_limited()
    if _lim:
        return _lim
    combo = [k.strip().lower() for k in args.keys.replace(" ", "").split("+") if k.strip()]
    if not combo:
        return ToolResult(ok=False, error=ToolError(code="bad_args", category="validation", message="no keys given"))
    if len(combo) == 1:
        pg.press(combo[0])
    else:
        pg.hotkey(*combo)
    return ToolResult(ok=True, data={"keys": combo})
