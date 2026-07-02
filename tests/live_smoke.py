"""Live smoke test — fires a battery of REAL requests through the whole stack (real LM Studio
brain + vision, real tools, real filesystem) and verifies each answer at the end.

This is NOT a pytest unit test: it needs LM Studio running with your brain loaded (and, for the
vision scenario, your vision model). It exercises actual tool functionality end to end and takes
roughly 5-10 minutes on a local model.

Run from the repo root:
    .venv312\\Scripts\\python tests\\live_smoke.py

Options:
    --skip-vision      skip the screen.look scenario (no vision model loaded)
    --skip-web         skip the web.search scenario (offline / web tool disabled)
    --only SUBSTR      run only scenarios whose name contains SUBSTR (repeatable-ish, one value)
    --timeout SECONDS  per-scenario timeout (default 180)
    --keep             keep the temp working directory instead of deleting it

Exit code is the number of FAILED scenarios (0 = all green).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

# Make the package importable when run as `python tests/live_smoke.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jarvis.app import Application            # noqa: E402
from jarvis.config import load_settings       # noqa: E402
from jarvis.model.client import Message       # noqa: E402

DENIAL = re.compile(
    r"(can'?t|cannot|can not|unable|not able|don'?t have|don'?t think i can|no way|impossible|"
    r"physical|missing\b[^.]*\bcapabilit|hardware)", re.I)


def _ans(res) -> str:
    return (getattr(res, "final_answer", None) or "").strip()


def _completed(res) -> bool:
    return getattr(res, "status", None) == "completed"


# --------------------------------------------------------------------------- #
# scenarios: each has name, message, check(res, ctx) -> (ok, detail), and flags.
# ctx carries {"workdir": Path} for filesystem assertions.
# --------------------------------------------------------------------------- #
def _scenarios():
    def math_check(res, ctx):
        a = _ans(res)
        return (_completed(res) and "51" in a, f"want '51' in answer; got {a!r}")

    def paragraph_check(res, ctx):
        a = _ans(res)
        ok = _completed(res) and len(a.split()) >= 12 and not DENIAL.search(a)
        return (ok, f"want a substantive non-refusal paragraph; got {a[:120]!r}")

    def write_check(res, ctx):
        a = _ans(res)
        f = ctx["workdir"] / "jarvis_test.txt"
        on_disk = f.exists() and "PING-4242" in f.read_text(encoding="utf-8", errors="ignore")
        return (_completed(res) and "PING-4242" in a and on_disk,
                f"file_on_disk={on_disk}, answer_has_token={'PING-4242' in a}; got {a[:120]!r}")

    def list_check(res, ctx):
        a = _ans(res).lower()
        return (_completed(res) and "jarvis_test" in a,
                f"want 'jarvis_test.txt' listed; got {a[:150]!r}")

    def disk_check(res, ctx):
        a = _ans(res)
        has_num = re.search(r"\d+(\.\d+)?\s*(kb|mb|gb|tb|bytes)", a, re.I)
        return (_completed(res) and bool(has_num),
                f"want a size with a unit (GB/MB/...); got {a[:150]!r}")

    def shell_check(res, ctx):
        a = _ans(res)
        return (_completed(res) and "2026" in a,
                f"want the current year 2026 from a shell command; got {a[:150]!r}")

    def web_check(res, ctx):
        a = _ans(res).lower()
        return (_completed(res) and "herbert" in a,
                f"want 'Herbert' (author of Dune); got {a[:150]!r}")

    def vision_check(res, ctx):
        a = _ans(res)
        # Content can't be asserted deterministically; require a real, grounded, non-refusal reply.
        return (_completed(res) and len(a) >= 8 and not DENIAL.search(a),
                f"want a real description of the screen (no 'I can't see'); got {a[:150]!r}")

    def capture_check(res, ctx):
        # A "remember this" that merely acknowledges (0 steps, no vault.capture) must NOT pass:
        # verify the fact was actually written to the vault.
        if not _completed(res):
            return False, f"capture run should complete; status={getattr(res,'status',None)}"
        app = ctx.get("app")
        try:
            notes = app.vault.all_notes() if app else []
            stored = any("bluefalcon" in ((n.body or "") + (n.title or "")).lower() for n in notes)
        except Exception as e:
            return False, f"could not check the vault: {e}"
        return stored, "run completed but 'BlueFalcon' was never written to the vault (only acknowledged)"

    def recall_check(res, ctx):
        a = _ans(res).lower().replace(" ", "")
        return ("bluefalcon" in a,
                f"want recalled codename 'BlueFalcon'; got {_ans(res)[:150]!r}")

    def refusal_check(res, ctx):
        a = _ans(res)
        return (_completed(res) and bool(DENIAL.search(a)),
                f"want a graceful refusal of a physical task; got {a[:150]!r}")

    return [
        # name, message, check, kind
        dict(name="math_direct",
             msg="What is 17 times 3? Reply with just the number.",
             check=math_check),
        dict(name="paragraph_open",
             msg="Write me a random two-sentence paragraph about the sea.",
             check=paragraph_check),
        dict(name="fs_write",   # must run before fs_list (shares the workdir)
             msg="Create a text file named jarvis_test.txt in my working directory whose exact "
                 "contents are PING-4242, then tell me what the file contains.",
             check=write_check),
        dict(name="fs_list",
             msg="List the files in my working directory and tell me their names.",
             check=list_check),
        dict(name="disk_usage",
             msg="How much free disk space is on the C: drive? Give me the number with its unit.",
             check=disk_check),
        dict(name="shell_date",
             msg="Run a shell command to get the current date, then tell me what year it is.",
             check=shell_check),
        dict(name="web_search",
             msg="Search the web for who wrote the science-fiction novel Dune and tell me the "
                 "author's full name.",
             check=web_check, kind="web"),
        dict(name="vision_screen",
             msg="Look at my screen right now and describe in one sentence what is currently "
                 "visible.",
             check=vision_check, kind="vision", timeout=240),
        dict(name="memory_capture",
             msg="Please remember this fact for later: my project codename is BlueFalcon.",
             check=capture_check),
        dict(name="memory_recall",
             msg="What is my project codename?",
             check=recall_check),
        dict(name="infeasible_refusal",
             msg="Physically unplug the monitor cable from the back of my computer.",
             check=refusal_check),
    ]


# --------------------------------------------------------------------------- #
# unattended safety nets: auto-approve confirmations, auto-answer any question
# --------------------------------------------------------------------------- #
async def _auto_approve(app, stop):
    while not stop.is_set():
        broker = getattr(app, "confirm", None)
        pending = getattr(broker, "_pending", {}) if broker else {}
        for cid, fut in list(pending.items()):
            if not fut.done():
                broker.resolve(cid, True)
        await asyncio.sleep(0.05)


async def _auto_answer(app, stop, asked):
    seen = set()
    while not stop.is_set():
        qs = getattr(app.loop, "_questions", {})
        for qid in list(qs.keys()):
            if qid not in seen:
                seen.add(qid)
                asked.append(qid)
                app.answer_question(qid, "Proceed with a sensible default.")
        await asyncio.sleep(0.05)


async def _run_one(app, sc, ctx, timeout):
    thread = f"live-{sc['name']}"
    req = app.new_run(sc["msg"], thread_id=thread, working_directory=str(ctx["workdir"]))
    t0 = time.time()
    try:
        res = await asyncio.wait_for(app.run_sync(req), timeout=sc.get("timeout", timeout))
    except asyncio.TimeoutError:
        return False, f"TIMEOUT after {sc.get('timeout', timeout)}s", time.time() - t0, 0, ""
    dt = time.time() - t0
    steps = getattr(res, "steps", 0)
    try:
        ok, detail = sc["check"](res, ctx)
    except Exception as e:                      # a checker blew up -> treat as failure, keep going
        ok, detail = False, f"checker error: {e}"
    return ok, detail, dt, steps, _ans(res)


async def main(args):
    settings = load_settings()
    settings.autonomy.confirm_level = "never"   # unattended: never block on approvals

    app = Application(settings)
    app.ensure_vault()

    base, _key, _timeout = app._brain_endpoint()
    brain_model = getattr(settings.role("brain"), "model", "?")
    vision_url = getattr(settings.models, "vision_base_url", "") or base
    print("=" * 78)
    print("JARVIS live smoke test")
    print(f"  brain : {base}  model={brain_model}")
    print(f"  vision: {vision_url}")
    print("=" * 78)

    # Preflight: is the brain reachable? Fail fast with a clear message rather than 11 timeouts.
    try:
        app.model.chat([Message(role="user", content="ping")], max_tokens=1, temperature=0.0)
    except Exception as e:
        print(f"\nERROR: could not reach the brain model at {base}\n  {e}\n"
              f"Start LM Studio, load your brain model, then re-run.")
        app.close()
        return 2

    # Config<->LM Studio sync check: confirm the model that will actually be sent is loaded.
    try:
        loaded = [m.id for m in app.model.list_models()]
    except Exception:
        loaded = []
    print("loaded on brain host: " + (", ".join(loaded) or "(none reported)"))
    if brain_model == "auto" and loaded:
        print(f"NOTE: brain model is 'auto' -> requests will use LM Studio's first model: {loaded[0]}")
    elif brain_model not in loaded and loaded:
        print(f"WARNING: configured brain '{brain_model}' is NOT loaded — LM Studio may 404 or run a "
              f"different model. Load it in LM Studio or fix config/local.yaml before trusting results.")
    print()

    workdir = Path(tempfile.mkdtemp(prefix="jarvis_live_"))
    ctx = {"workdir": workdir, "app": app}
    print(f"working dir: {workdir}\n")

    scenarios = _scenarios()
    if args.skip_vision:
        scenarios = [s for s in scenarios if s.get("kind") != "vision"]
    if args.skip_web:
        scenarios = [s for s in scenarios if s.get("kind") != "web"]
    if args.only:
        scenarios = [s for s in scenarios if args.only.lower() in s["name"].lower()]

    stop = asyncio.Event()
    asked: list = []
    nets = [asyncio.create_task(_auto_approve(app, stop)),
            asyncio.create_task(_auto_answer(app, stop, asked))]

    results = []
    t_all = time.time()
    for i, sc in enumerate(scenarios, 1):
        print(f"[{i:>2}/{len(scenarios)}] {sc['name']:<20} ...", end="", flush=True)
        ok, detail, dt, steps, answer = await _run_one(app, sc, ctx, args.timeout)
        results.append((sc["name"], ok, detail, dt, answer))
        print(f" {'PASS' if ok else 'FAIL'}  ({dt:>5.1f}s, {steps} steps)")
        if not ok:
            print(f"         reason: {detail}")

    stop.set()
    for t in nets:
        t.cancel()
    await asyncio.gather(*nets, return_exceptions=True)

    passed = sum(1 for _, ok, *_ in results if ok)
    failed = len(results) - passed
    print("\n" + "=" * 78)
    print(f"RESULTS: {passed} passed, {failed} failed in {time.time() - t_all:.0f}s")
    if failed:
        print("failed scenarios:")
        for name, ok, detail, _dt, answer in results:
            if not ok:
                print(f"  - {name}: {detail}")
    if asked:
        print(f"note: {len(asked)} clarifying question(s) were auto-answered during the run.")
    print("=" * 78)

    if args.keep:
        print(f"(kept working dir: {workdir})")
    else:
        shutil.rmtree(workdir, ignore_errors=True)
    app.close()
    return failed


def _parse():
    p = argparse.ArgumentParser(description="JARVIS live end-to-end smoke test")
    p.add_argument("--skip-vision", action="store_true")
    p.add_argument("--skip-web", action="store_true")
    p.add_argument("--only", default=None)
    p.add_argument("--timeout", type=int, default=180)
    p.add_argument("--keep", action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    try:
        rc = asyncio.run(main(_parse()))
    except KeyboardInterrupt:
        rc = 130
    sys.exit(min(rc or 0, 125))
