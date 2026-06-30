# JARVIS — backend (PLANv3)

Local-first personal AI assistant backend. Implements the PLANv3 spine:
LM Studio model gateway, typed tool system with subsetting, a ReAct agent loop
with governors + kill switch, reversible filesystem/shell tools, a FastAPI + SSE
API, and the headline of PLANv3 — a **plain-text Markdown "second brain" vault**
as canonical memory (profile auto-load, wikilink graph, hybrid retrieval,
projects, skills, and a gardener).

The web/app UI is intentionally **not** built yet (per request) — confirm the
design first, then the UI attaches to this API.

## Requirements

- Python 3.10+ (3.12 recommended).
- [LM Studio](https://lmstudio.ai) running its local server at
  `http://127.0.0.1:1234/v1` with a tool-calling-capable model loaded
  (e.g. a Qwen-class ~9–14B). Verify your GPU with `nvidia-smi` (the desktop
  RTX 3060 is 12 GB).

## Setup

```bash
cd alpha/prod
python -m venv .venv
. .venv/Scripts/activate          # Windows PowerShell:  .venv\Scripts\Activate.ps1
pip install -e ".[dev]"           # add ".[dev,embed,vector]" for local embeddings/vector search
```

## Run

```bash
# Health check (talks to LM Studio):
python -m jarvis.main health

# One-shot headless task (uses LM Studio):
python -m jarvis.main run "create hello.txt in this folder that says hi" --cwd .

# Start the API server (http://127.0.0.1:8765):
python -m jarvis.main serve
```

Key endpoints: `GET /api/health`, `GET /api/models`, `POST /api/runs`,
`GET /api/runs/{id}/events` (SSE), `POST /api/runs/{id}/cancel`, `GET /api/tools`,
`POST /api/vault/search`, `GET /api/vault/profile`, `GET /api/audit`,
`POST /api/rollback/{token}`, `GET /api/settings`.

## Tests

```bash
pytest                 # full suite (needs deps installed)
```

The suite covers: vault I/O + frontmatter + wikilink graph + hybrid retrieval +
gardener + reindex; reversible fs tools (snapshot/trash + rollback); shell
tripwire + execution; tool subsetting; the agent loop end-to-end via a
`FakeModelClient` (no LM Studio needed); step-budget and no-progress governors;
redaction; and the health/tools API.

## Configuration

Defaults live in `config/default.yaml`. Override via `config/local.yaml`
(git-ignored) or env vars (`JARVIS__SECTION__KEY=value`). Point `vault.path` at a
real location (e.g. `C:/Users/<you>/JarvisBrain`) and, optionally, set
`vault.git_versioning: true` and `vault.embed.mode: lmstudio` (or `in_process`
with the `embed` extra) to enable vector retrieval.

## Layout

```
jarvis/
  config.py          # layered typed settings
  events.py          # Event + EventSink (durable + realtime fanout)
  app.py             # composition root (wires everything)
  main.py            # jarvisd entrypoint + tiny CLI
  model/             # ModelClient protocol, LM Studio adapter, FakeModelClient, context
  tools/             # contract, registry+subsetting, executor, fs/shell/meta/vault tools
  memory/            # vault (canonical Markdown), index (SQLite catalog + links), graph, retrieval, gardener
  agent/             # run state, ReAct loop with governors/kill switch, prompts
  security/          # redaction, policy/tripwires, rollback manager
  data/              # SQLite operational store
  api/               # FastAPI server + SSE
config/              # default.yaml
tests/               # unit + integration + end-to-end (FakeModelClient)
```

## Design notes

- **Memory is plain text you own.** The vault Markdown is canonical; the SQLite
  note catalog (with content hashes for change detection), the persisted wikilink
  edges, and any vector index are rebuildable caches (`vault.reindex`). Swap the
  model and the brain survives.
- **Reversible by default.** Overwrites snapshot; deletes go to the Recycle Bin;
  bulk moves record an undo manifest — all rollback-able from `/api/rollback`.
- **Keys, not prompts.** Tripwires and path scope are enforced in code; future
  live-data connectors are read-only/scoped, not governed by prompt text.
- **Swappable runtime.** Everything talks OpenAI-compatible; point `base_url` at
  Ollama/llama.cpp without code changes.

## Not yet implemented (deferred by design)

Browser (Playwright) and Windows GUI (pywinauto/UIA) tools, voice, the scheduler
daemon, MCP client, and the web UI. The interfaces and namespaces are in place so
these attach without disturbing the core.
