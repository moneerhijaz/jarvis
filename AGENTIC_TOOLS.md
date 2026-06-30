# JARVIS — Agentic Tools (computer control, web, vision)

Reference for the agentic capabilities added per `AGENTIC_PLAN.md`. All OS/web libraries
are lazy-imported, so the backend boots without them; `GET /api/health` → `capabilities`
reports what's installed.

## Install (Windows, into .venv312)
```powershell
pip install -e ".[control]"          # mss, Pillow, pyautogui, pygetwindow, uiautomation, keyboard
pip install -e ".[ocr,websearch]"    # optional: RapidOCR (screen.read_text) + ddgs (web.search)
```
Load a vision model (e.g. **Qwen3-VL**) in LM Studio for `screen.look` and the vision
half of `ui.click`. Pick it in the UI **VISION** dropdown (or leave Auto to auto-detect).

## Tools
- **screen.look(question?)** — vision model *describes* the screen / answers a question. ("what's on my screen")
- **screen.read_text(display?, region?)** — OCR to text **+ clickable item coordinates**.
- **screen.capture(display?, region?)** — save a PNG (auto-purged after 10 min).
- **screen.regions()** — list displays + bounds.
- **ui.click("the Save button", window?)** — hybrid click: accessibility tree first, vision fallback. No coordinate guessing.
- **ui.find(target)** / **ui.read(window?)** — locate without clicking / list a window's elements.
- **input.move/click/drag/scroll/type/press/position** — raw mouse+keyboard (clamped to the desktop, rate-limited, corner-failsafe ON).
- **win.launch/list/focus/move/state** — apps & windows. `win.move` is undoable (rollback token).
- **sys.run_admin(command)** — UAC-elevated command (output captured). **sys.power(action)** — lock/sleep/shutdown/restart.
- **web.search(query)** / **web.fetch(url)** — internet search + readable page text.

## Safety model
Permissions are set live from the **PERMISSIONS** dropdown (persists to `config/local.yaml`):
- **Full control** (`never`) — nothing asks. Admin runs instantly.
- **Confirm risky** (`risky`, default) — admin/destructive/system actions show an Approve/Deny card.
- **Confirm everything** (`every`) — every tool call asks.

Beyond the risk class, these **force** a confirmation (unless level = Full control):
- typing a value that looks like a **secret/API key/private key**,
- acting while the **active window looks like a login/payment** screen,
- **closing a window** (`win.state close`).

The confirmation card shows the exact action + reason and a **countdown**; no answer →
**auto-deny**. In voice/LIVE mode the action is read aloud and you can say **"yes" / "no."**

Hard guardrails (independent of the model):
- **Global kill switch:** `Ctrl+Alt+Esc` (config `autonomy.kill_hotkey`) → cancels every run, kills child processes, clears pending confirms. Also `POST /api/panic`, the UI Stop, and pyautogui's corner-failsafe.
- **Input clamping + rate limit:** coordinates clamped to the virtual desktop; ≤25 input actions/sec.
- **Feedback-loop guards:** JARVIS's own window is excluded from `win.*` and `ui.*`; continuous-listen ignores the mic while JARVIS is busy.
- **Tripwires:** catastrophic shell patterns (format, diskpart, reg delete, rm -rf /) are refused for `shell.run`/`sys.run_admin`.
- **Audit + privacy:** every action is logged (review/undo in the **ACTIONS** panel). Screenshots auto-purge after 10 min; on-screen/OCR text is kept out of the audit log.

## Config (config/local.yaml)
```yaml
autonomy:
  confirm_level: risky        # every | risky | never  (the PERMISSIONS dropdown writes this)
  kill_hotkey: ctrl+alt+esc
models:
  vision_model: ""            # blank = auto-detect a VL model; or pin an id (the VISION dropdown writes this)
```

## Known limits
- Vision locator sees the **primary** monitor, downscaled to 1280px wide.
- The brain reasons over text; it *sees* only via `screen.look`/`screen.read_text` (no images in its raw context).
- `input.*` actions are irreversible (only `win.move` has rollback).
