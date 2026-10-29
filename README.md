# JARVIS

A local-first personal AI assistant with a Python backend, React web console,
OpenAI-compatible model gateway, and a plain-text Markdown memory vault.
It includes a staged agent pipeline, filesystem and shell tools, optional
Windows desktop control, and optional local speech recognition and synthesis.

This is experimental software. The API has no authentication and can execute
commands and access local files. Keep it bound to `127.0.0.1`; do not expose it
to the internet or an untrusted network. Review tool permissions before use.

## Requirements

- Python 3.10+ (3.12 recommended).
- LM Studio or an OpenAI-compatible local model server, normally at
  `http://127.0.0.1:1234/v1`, with a tool-calling model loaded.
- Node.js 20.19+ for the optional React UI.
- Windows for Windows-specific desktop automation tools.

## Quick start

Run from the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
python -m jarvis.main serve
```

Open `http://127.0.0.1:8765`. The backend serves the built React UI when
`ui/dist` exists, otherwise its bundled web interface.

```bash
# Optional React UI build
cd ui
npm ci
npm run build
```

For UI development, run `npm run dev` from `ui` while the backend is running.
See [UI instructions](ui/README.md) for voice setup and development details.

```bash
python -m jarvis.main health
python -m jarvis.main run "create hello.txt in this folder that says hi" --cwd .
```

## Configuration and privacy

Copy `config/local.yaml.example` to `config/local.yaml` for local overrides.
Defaults live in `config/default.yaml`; environment overrides use
`JARVIS__SECTION__KEY=value`. Run commands from the repository root so the
configuration and relative data paths resolve consistently.

Conversation databases and the memory vault live under `data/`; screenshots
and other outputs live under `artifacts/`. These directories, logs, credentials,
and local configuration are ignored by Git. Review files before committing:
ignore rules do not protect files already tracked or explicitly force-added.

Optional install extras include `voice`, `kokoro`, `embed`, `vector`, `control`,
`ocr`, and `websearch`. Models must be downloaded separately. Core tests use
fake model clients and do not require LM Studio.

## Development

```bash
pytest -q
cd ui
npm ci
npm run build
```

See [contributing](CONTRIBUTING.md) and [security](SECURITY.md) for contribution
and vulnerability reporting guidance. Historical design documents in `plan/`
and the root planning files describe proposals as well as implemented features.

## Layout

- `jarvis/api/`: FastAPI API and event streaming.
- `jarvis/pipeline/`: agent planning and execution pipeline.
- `jarvis/tools/`: filesystem, shell, browser, desktop, and memory tools.
- `jarvis/memory/`: Markdown vault, indexing, retrieval, and maintenance.
- `jarvis/security/`: policy checks, redaction, and rollback support.
- `jarvis/voice/`: optional local speech services.
- `ui/`: React + Vite console.
- `tests/`: automated checks and separate manual live-model scripts.

See [implementation boundaries](docs/PORTFOLIO.md) for the component diagram and what core tests do not establish.

## License

See [LICENSE](LICENSE) for the GNU General Public License, version 3.
