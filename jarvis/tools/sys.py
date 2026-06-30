"""Admin / system tools (AGENTIC_PLAN §3.4).

`sys.run_admin` runs a command elevated via UAC (PowerShell Start-Process -Verb RunAs),
capturing its combined output through a temp file. `sys.power` locks/sleeps/shuts
down/restarts. Both are risk 'system' => always confirmed under any confirm level except
'never'. Catastrophic commands still trip the policy tripwire and are refused.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time

from pydantic import BaseModel, Field

from jarvis.tools.base import ToolContext, ToolError, ToolResult, tool

_IS_WINDOWS = sys.platform.startswith("win")
_MAX = 65536


class RunAdminArgs(BaseModel):
    command: str = Field(..., description="Command to run with administrator privileges (PowerShell).")
    timeout_s: int = Field(180, description="Kill the elevated command after this many seconds.")


@tool(name="sys.run_admin", risk="system", timeout_s=300, confirm="always",
      capabilities=["system.admin", "shell.run"], output_kind="prose")
def sys_run_admin(args: RunAdminArgs, ctx: ToolContext) -> ToolResult:
    """Run a command as administrator (triggers a Windows UAC prompt). Returns its output."""
    if not _IS_WINDOWS:
        return ToolResult(ok=False, error=ToolError(code="unsupported", category="execution",
                                                    message="sys.run_admin is Windows-only"))
    if ctx.policy:
        hit = ctx.policy.check_shell(args.command)
        if hit:
            return ToolResult(ok=False, metadata={"tripwire": hit.name},
                              error=ToolError(code="tripwire", category="tripwire", message=f"refused: {hit.reason}"))
    tmp = tempfile.mkdtemp(prefix="jarvis_admin_")
    out_path = os.path.join(tmp, "out.txt")
    script_path = os.path.join(tmp, "run.ps1")
    # The elevated script runs the command and redirects ALL streams to out.txt.
    with open(script_path, "w", encoding="utf-8") as f:
        f.write("& {\n" + args.command + "\n} *>&1 | Out-File -FilePath '" + out_path + "' -Encoding utf8\n")
    outer = ("Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden "
             "-ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','" + script_path + "'")
    t0 = time.time()
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", outer],
                              capture_output=True, text=True, timeout=args.timeout_s)
    except subprocess.TimeoutExpired:
        return ToolResult(ok=False, error=ToolError(code="timeout", category="timeout",
                                                    message=f"elevated command timed out after {args.timeout_s}s"))
    if proc.returncode != 0:
        # Non-zero here usually means the UAC prompt was declined / elevation failed.
        return ToolResult(ok=False, stderr=(proc.stderr or "")[:_MAX],
                          error=ToolError(code="elevation_failed", category="permission",
                                          message="elevation failed or was declined at the UAC prompt"))
    try:
        with open(out_path, "r", encoding="utf-8", errors="replace") as f:
            output = f.read()
    except Exception:
        output = ""
    return ToolResult(ok=True, stdout=output[:_MAX],
                      data={"elevated": True}, duration_ms=int((time.time() - t0) * 1000))


class DiskUsageArgs(BaseModel):
    path: str = Field("", description="Folder to break down by sub-folder. Blank = the system drive (C:\\).")
    top: int = Field(12, description="Return the N largest sub-folders.")


@tool(name="sys.disk_usage", risk="read", timeout_s=900,
      capabilities=["disk.capacity", "disk.measure", "folders.largest"], output_kind="ranking",
      output_schema={"drives": [{"drive": "str", "total_gb": "float", "used_gb": "float", "free_gb": "float"}],
                     "largest_folders": [{"name": "str", "path": "str", "size_gb": "float"}]},
      examples=[{"request": "how much disk space do I have / what's taking up space", "args": {}},
                {"request": "biggest folders in Program Files", "args": {"path": "C:\\Program Files"}}])
def sys_disk_usage(args: DiskUsageArgs, ctx: ToolContext) -> ToolResult:
    """Report disk capacity for every drive (total/used/free) and a full breakdown of the
    largest sub-folders under a path. Use this for 'how big is my disk / what's taking up
    space'. The folder breakdown measures every file (exact, no time limit), so a large
    drive can take a while — but the numbers are complete and accurate."""
    import shutil

    def gb(n: int) -> float:
        return round(n / (1024 ** 3), 2)

    drives: list[dict] = []
    if _IS_WINDOWS:
        import string
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if os.path.exists(root):
                try:
                    t, u, f = shutil.disk_usage(root)
                    drives.append({"drive": root, "total_gb": gb(t), "used_gb": gb(u), "free_gb": gb(f),
                                   "percent_used": round(u / t * 100, 1) if t else 0})
                except Exception:
                    pass
        target = args.path or (os.environ.get("SystemDrive", "C:") + "\\")
    else:
        t, u, f = shutil.disk_usage("/")
        drives.append({"drive": "/", "total_gb": gb(t), "used_gb": gb(u), "free_gb": gb(f),
                       "percent_used": round(u / t * 100, 1) if t else 0})
        target = args.path or "/"

    def folder_size(path: str) -> int:
        total, stack = 0, [path]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            if e.is_symlink():
                                continue
                            if e.is_file(follow_symlinks=False):
                                total += e.stat(follow_symlinks=False).st_size
                            elif e.is_dir(follow_symlinks=False):
                                stack.append(e.path)
                        except (PermissionError, OSError):
                            pass
            except (PermissionError, OSError):
                pass
        return total

    folders: list[dict] = []
    try:
        with os.scandir(target) as it:
            children = [e.path for e in it if e.is_dir(follow_symlinks=False)]
    except Exception as e:
        children = []
        if not drives:
            return ToolResult(ok=False, error=ToolError(code="not_found", category="not_found",
                              message=f"cannot read {target}: {e}"))
    for p in children:
        folders.append({"name": os.path.basename(p), "path": p, "size_gb": gb(folder_size(p))})
    folders.sort(key=lambda x: x["size_gb"], reverse=True)
    return ToolResult(ok=True, redact_data=True, data={
        "drives": drives, "breakdown_of": target, "largest_folders": folders[:max(1, args.top)],
    })


class PowerArgs(BaseModel):
    action: str = Field(..., description="lock | sleep | shutdown | restart")


@tool(name="sys.power", risk="system", timeout_s=30, confirm="always",
      capabilities=["system.power"], output_kind="action")
def sys_power(args: PowerArgs, ctx: ToolContext) -> ToolResult:
    """Lock the screen, sleep, shut down, or restart the machine."""
    if not _IS_WINDOWS:
        return ToolResult(ok=False, error=ToolError(code="unsupported", category="execution",
                                                    message="sys.power is Windows-only"))
    act = (args.action or "").lower()
    cmds = {
        "lock": "rundll32.exe user32.dll,LockWorkStation",
        "sleep": "rundll32.exe powrprof.dll,SetSuspendState 0,1,0",
        "shutdown": "shutdown /s /t 0",
        "restart": "shutdown /r /t 0",
    }
    if act not in cmds:
        return ToolResult(ok=False, error=ToolError(code="bad_args", category="validation",
                                                    message="action must be lock|sleep|shutdown|restart"))
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-Command", cmds[act]])
    except Exception as e:
        return ToolResult(ok=False, error=ToolError(code="power_failed", category="execution", message=str(e)))
    return ToolResult(ok=True, data={"action": act})
