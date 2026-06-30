# JARVIS — Agentic Computer-Control Plan (AGENTIC_PLAN.md)

Status: **planning only — no code yet.** This document specifies how to give JARVIS
the ability to see the screen, control mouse/keyboard, run shell/admin commands, and
manage apps/windows on the local Windows machine, wired into the existing tool system
with a "confirm risky only" safety gate.

---

## 1. Goal & scope

Add four capability groups as new tools:

1. **Screen capture** — let JARVIS "see" the display(s).
2. **Mouse & keyboard** — move/click/scroll/type/hotkeys ("tap on things").
3. **Shell / admin commands** — run programs and elevated/system commands.
4. **App & window control** — launch apps, focus/switch/move/resize windows.

Everything runs in the **local backend process on Windows** (the `.venv312` that already
serves the API), which has access to the interactive desktop. The Linux sandbox is not
involved. Voice/UI already exist; this plan only adds backend tools + a confirmation UI.

Out of scope for the first pass: multi-machine control, remote desktops, scheduled
autonomous operation, and anything touching another person's device.

---

## 2. How it fits the current architecture (anchors)

The codebase already has the right spine, so this is mostly *new tools + one new gate*:

- **Tool contract** (`jarvis/tools/base.py`): `@tool(name, risk, timeout_s, cancellable,
  always_on)` → `ToolSpec`. `Risk = "read" | "write" | "destructive" | "system"`. Tools
  self-register; `Registry.select_for(goal)` subsets them by namespace/keyword hints.
- **Executor** (`jarvis/tools/executor.py`): validates args, runs the handler in a worker
  thread with a timeout, emits `tool.started/completed/failed`, and writes an audit row
  with the tool's `risk`. This is where every action is already logged.
- **Loop** (`jarvis/agent/loop.py`): streams the model, runs each `tool_call` through the
  executor, feeds the JSON observation back. Governors already present: `max_run_steps`,
  `max_tool_failures`, cancel checks, and a no-progress signature detector.
- **Policy / tripwires** (`jarvis/security/policy.py`): refuses catastrophic shell commands
  and protected paths; `shell.run` already calls `policy.check_shell`.
- **Autonomy config** (`jarvis/config.py::AutonomyCfg`): `mode`, `audit_all_tools`,
  `enable_kill_switch`, `rollback_where_possible`, `tripwires_enabled`, `delete_threshold`.
- **Events** (`jarvis/events.py`): durable pub/sub spine the SSE endpoint streams. We add
  two event types for confirmations.
- **ToolContext**: already carries `policy`, `rollback`, `store`, `settings`, `processes`
  (for kill), `cancelled()`. We add a `confirm(...)` hook here.

**Design principle to preserve:** safety is *capability control in code*, not prompt text.
The model can request anything; the gate/policy decides what actually executes.

---

## 3. New tool namespaces & tools

New modules under `jarvis/tools/`:

### 3.1 `screen.py` — vision input (risk: `read`)
- `screen.capture(display?, region?, scale?)` → returns a PNG **artifact** + metadata
  (resolution, display list, scale factor). Library: `mss` (fast) or Pillow `ImageGrab`.
- `screen.regions()` → list displays and their bounds (multi-monitor aware).
- `screen.read_text(region?)` → OCR text + bounding boxes (only if we adopt the OCR path,
  see §4). Library: `pytesseract` (needs Tesseract) or `rapidocr-onnxruntime` (self-contained).

Screenshots are **read** risk → never gated, but see privacy controls in §6.

### 3.2 `input.py` — mouse & keyboard (risk: `write`)
- `input.move(x, y, duration?)`, `input.click(x?, y?, button?, double?)`,
  `input.drag(x1,y1,x2,y2)`, `input.scroll(dx, dy)`.
- `input.type(text)`, `input.press(keys)` (e.g. `"ctrl+s"`, `"enter"`).
- Library: **`pyautogui`** (cross-platform, has a built-in corner **failsafe**) and/or
  `pydirectinput` for games/low-level. Coordinates are absolute screen pixels; helpers
  clamp to display bounds and respect a per-action max move speed.
- Risk `write` → auto-runs under "confirm risky only". (These are not individually
  reversible, so the governors in §6 — rate limit, failsafe, kill switch — matter here.)

### 3.3 `win.py` — app & window control (risk: `write`)
- `win.launch(app, args?)` — start a program (Start-menu name, path, or URI).
- `win.list()` — enumerate visible top-level windows (title, pid, rect, state).
- `win.focus(match)`, `win.move(match, x, y, w, h)`, `win.state(match, min|max|restore|close)`.
- Library: **`pywinauto`** or **`uiautomation`** (both wrap Windows UI Automation) plus
  `pygetwindow`. `win.close` is `write` but borderline — treat unsaved-data closes as
  confirmable (heuristic) or leave `write` for v1.

### 3.4 `sys.py` — shell / admin (risk: `system` for elevated)
- Reuse the existing `shell.run` for normal commands (already `write` + tripwires).
- `sys.run_admin(command)` — run elevated via UAC (`Start-Process -Verb RunAs`) or a
  pre-authorized elevated helper. Risk `system` → **always confirmed**. Tripwires from
  `policy.check_shell` still apply (format, diskpart, reg delete, rm -rf /, etc.).
- `sys.power(action)` (lock/sleep/shutdown/restart) — `system`, confirmed.

### 3.5 Registration & subsetting
- Add namespace hints to `Registry.select_for`: `screen` ↔ {see, look, screenshot, screen,
  show}; `input` ↔ {click, type, press, tap, move, drag, scroll}; `win` ↔ {open, launch,
  window, app, focus, close, minimize}; `sys`/admin ↔ {admin, elevate, install, service,
  shutdown, registry}.
- `screen.capture` likely `always_on` (cheap, frequently needed for grounding).

---

## 4. **OPEN DECISION — how JARVIS decides *where* to click** (to discuss)

This is the core technical fork and is **not yet decided**. Options:

- **A. Windows accessibility tree (UI Automation).** Read real UI elements (name, role,
  bounds) via `pywinauto`/`uiautomation`; click by element, not pixels. *Pros:* precise,
  reliable, no vision model, fast, self-describing to the LLM. *Cons:* some apps expose
  poor/no automation tree (custom-drawn UIs, games, some Electron apps), Office/web vary.
- **B. Vision model + pixel coordinates.** Send `screen.capture` to a **multimodal LLM**
  that returns coordinates to click. *Pros:* works on literally anything visible. *Cons:*
  needs a vision-capable model loaded in LM Studio, can mis-aim, slower, needs the
  multimodal plumbing in §5.
- **C. Hybrid (recommended).** Try the accessibility tree first; fall back to vision when
  an element can't be found. Most capable, most work.
- **D. OCR + screenshot.** Find on-screen text and click it. No vision LLM; limited to
  text targets.

**Recommendation:** start with **A (accessibility tree)** because it's deterministic and
needs no new model, then add **B** as a fallback to reach **C**. But this depends on what
model you can run: **does LM Studio have a vision/multimodal model available?** If not,
B/C are blocked until one is added, and we'd ship A+D first.

**Action: confirm the targeting approach and the available vision model before Phase 2.**

---

## 5. Multimodal plumbing (only if we adopt vision, option B/C)

Today tool results are JSON observations; an image can't flow back to the model as text.
To let the model *see* a screenshot we need:

- **Model gateway change** (`jarvis/model/lmstudio.py`): support OpenAI-style image content
  blocks (`{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}`) in
  messages, gated by a `model.vision: true` capability flag in config.
- **Loop change**: when a tool returns an image artifact and the active model is multimodal,
  inject the image as the content of the `tool`/`user` observation message (downscaled to a
  sane width, e.g. ≤1280px, to control tokens/latency).
- **Config**: `models.vision_model` (a dedicated multimodal model to route screen tasks to,
  independent of the text brain) + auto-routing so "look at my screen" picks it.

This is the largest single piece of work and is **deferred behind the §4 decision**.

---

## 6. Safety model — "confirm risky only" + guardrails

You chose **confirm risky only**: reads/screenshots/benign clicks run freely; admin
commands, deletes, power actions, and other high-risk steps require explicit approval.

### 6.1 Risk → gate mapping
- `read` (screen capture, window list, OCR) → **auto**.
- `write` (mouse, keyboard, window move/focus, normal shell) → **auto**.
- `destructive` (bulk delete, overwrite, `win.close` w/ unsaved heuristic) → **confirm**.
- `system` (`sys.run_admin`, `sys.power`, registry/service edits) → **confirm**.
- Per-tool override: a `confirm: "always" | "risky" | "never"` field on `ToolSpec` so we can
  force-confirm a specific `write` tool (e.g. `input.type` into a password field) regardless
  of the global setting.

### 6.2 Confirmation flow (new)
- Add `AutonomyCfg.confirm_level: "every" | "risky" | "never" = "risky"`.
- Add events: `action.confirm_requested` (payload: call_id, tool, args-redacted, risk,
  rationale) and `action.confirm_resolved` (approved/denied, by, ms).
- **Gate in the loop before `executor.execute`** (the executor stays sync-in-thread): if a
  call needs confirmation, emit `confirm_requested`, `await` a `Future` keyed by
  `(run_id, call_id)`, and only execute on approval. **Default on timeout (e.g. 60s) or on
  no UI connected = DENY.**
- **API**: `POST /api/runs/{run_id}/confirm` `{call_id, approved}` resolves the future.
- **UI**: a confirmation card over the core / in the chat panel showing the exact action
  (e.g. "Run as admin: `winget upgrade --all`") with Approve / Deny, plus a visible
  countdown. Voice mode: read the action aloud and accept "yes/no".

### 6.3 Hard guardrails (independent of the model)
- **Kill switch** (`enable_kill_switch` already true): a always-visible **STOP** that (a)
  cancels the run, (b) kills child processes (`ctx.processes`), and (c) **releases
  mouse/keyboard control immediately**. Bind a global hotkey too (e.g. `Ctrl+Alt+Esc`).
- **pyautogui failsafe**: slamming the cursor to a screen corner aborts input — keep ON.
- **Tripwires** (`policy.py`): extend beyond shell — refuse clicks/typing into known
  credential/payment surfaces where detectable; block `input.type` of obviously secret-like
  strings unless confirmed; keep the catastrophic-command list for `sys.run_admin`.
- **Rate limiting / loop safety**: cap actions/sec for `input.*`, and the existing
  `max_run_steps` / `max_tool_failures` / no-progress detector already stop runaway loops.
- **Audit** (already): every action is persisted with redaction + risk; add a session
  "action log" view in the UI for after-the-fact review and rollback where possible.
- **Privacy on screenshots**: store captures in a session-scoped temp dir, auto-purge on
  run end; optional region masking; never upload anywhere (local-only, matches the offline
  posture). Redact captured text in audit by default.

---

## 7. Dependencies (Windows, into `.venv312`)
- Screenshot: `mss`, `Pillow`.
- Input: `pyautogui` (+ optional `pydirectinput`).
- Windows/UIA: `pywinauto` and/or `uiautomation`, `pygetwindow`.
- OCR (only if option D): `rapidocr-onnxruntime` (self-contained, offline) or `pytesseract`
  (needs the Tesseract binary).
- Elevation: no package — use PowerShell `Start-Process -Verb RunAs` (UAC prompt).
- All optional/lazy-imported like the voice deps, so the backend still boots without them
  and reports capability availability via `/api/health`.

---

## 8. Phased roadmap (each phase = shippable + tested)

- **Phase 0 — Safety rail first.** `confirm_level` config, confirm events, loop gate,
  `/confirm` endpoint, UI confirmation card, kill-switch hotkey + input-release. *No control
  tools yet* — build the brakes before the engine. Tests: a fake `system` tool is gated;
  deny/timeout blocks execution; kill switch cancels mid-run.
- **Phase 1 — Eyes.** `screen.py` (`capture`, `regions`). Artifacts + health reporting.
  Tests: capture returns a valid PNG and dimensions on a headed Windows session.
- **Phase 2 — Targeting (after §4 decision).** Accessibility tree read + element click
  (`win.list`/element click), and/or vision plumbing (§5) if a multimodal model is chosen.
- **Phase 3 — Hands.** `input.py` mouse/keyboard with clamps, failsafe, rate limit.
- **Phase 4 — Apps & windows.** `win.py` launch/focus/move/state.
- **Phase 5 — Admin.** `sys.run_admin`, `sys.power`, tripwire extensions — all confirmed.
- **Phase 6 — Polish.** Action-log UI, rollback hooks where possible, auto-routing screen
  tasks to the vision model, docs.

---

## 9. Open questions to resolve before building
1. **Targeting approach (§4)** — accessibility tree vs vision vs hybrid vs OCR.
2. **Is a vision/multimodal model available in LM Studio?** (gates §5 and option B/C).
3. **Admin elevation UX** — accept a UAC prompt each time, or set up one pre-authorized
   elevated helper at startup? (security vs convenience trade-off).
4. **Kill-switch hotkey** choice and whether to also add a physical/voice "STOP".
5. **Multi-monitor** default: capture primary only, or all displays stitched?

## 10. Top risks
- **Mis-clicks / wrong target** (esp. vision) acting on the real desktop — mitigated by
  confirm-risky, failsafe, kill switch, and starting with the deterministic accessibility
  path.
- **Elevated commands** — highest blast radius; always confirmed + tripwires + audit.
- **Feedback loops** (JARVIS reacting to its own UI) — exclude JARVIS's own window from
  capture/targeting; pause continuous-listen during input bursts.
- **Latency** from vision screenshots — downscale, cache, prefer accessibility tree.
