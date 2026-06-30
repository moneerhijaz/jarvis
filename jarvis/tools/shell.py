"""Shell tools (PLANv3 / PLANv2 §7.9).

Runs commands with an explicit cwd, a timeout, output capture, and cancellation
(the running process is registered on the context so the kill switch / timeout
can terminate it). Uses PowerShell on Windows, /bin/sh elsewhere. Catastrophic
commands trip a policy tripwire and are refused rather than silently run.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time

from pydantic import BaseModel, Field

from jarvis.tools.base import ToolContext, ToolError, ToolResult, tool

_IS_WINDOWS = sys.platform.startswith("win")
_MAX_INLINE = 65536


class RunArgs(BaseModel):
    command: str = Field(..., description="The command to execute.")
    cwd: str | None = Field(None, description="Working directory.")
    timeout_s: int = Field(120, description="Kill the command after this many seconds.")


@tool(name="shell.run", risk="write", timeout_s=305, cancellable=True, always_on=True,
      capabilities=["shell.run", "system.command"], output_kind="prose",
      output_schema={"stdout": "str", "exit_code": "int"},
      examples=[{"request": "run a command", "args": {"command": "echo hello"}}])
def shell_run(args: RunArgs, ctx: ToolContext) -> ToolResult:
    """Run a shell command and return exit code, stdout, and stderr."""
    # Tripwire: refuse catastrophic operations (capability control, not a prompt).
    if ctx.policy:
        hit = ctx.policy.check_shell(args.command)
        if hit:
            return ToolResult(
                ok=False,
                error=ToolError(code="tripwire", message=f"refused: {hit.reason}", category="tripwire"),
                metadata={"tripwire": hit.name},
            )

    cwd = args.cwd or ctx.working_directory or os.getcwd()
    if _IS_WINDOWS:
        cmd = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", args.command]
    else:
        cmd = ["/bin/sh", "-c", args.command]

    t0 = time.time()
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=not _IS_WINDOWS,
        )
    except Exception as e:
        return ToolResult(ok=False, error=ToolError(code="spawn_failed", message=str(e), category="execution"))

    ctx.processes.append(proc)
    try:
        out, err = proc.communicate(timeout=args.timeout_s)
        code = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        return ToolResult(
            ok=False,
            error=ToolError(code="timeout", message=f"timed out after {args.timeout_s}s", category="timeout"),
            stdout=(out or "")[:_MAX_INLINE], stderr=(err or "")[:_MAX_INLINE],
            duration_ms=int((time.time() - t0) * 1000),
        )
    finally:
        if proc in ctx.processes:
            ctx.processes.remove(proc)

    return ToolResult(
        ok=(code == 0),
        data={"exit_code": code, "cwd": cwd},
        stdout=(out or "")[:_MAX_INLINE],
        stderr=(err or "")[:_MAX_INLINE],
        exit_code=code,
        duration_ms=int((time.time() - t0) * 1000),
        error=None if code == 0 else ToolError(code="nonzero_exit", message=f"exit {code}", category="execution"),
    )
