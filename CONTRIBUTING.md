# Contributing

Open an issue describing a bug or proposed change, then submit a focused pull
request with reproduction steps and the checks you ran. Use the setup commands
in README.md. Run `pytest -q` for backend changes and `npm ci && npm run build`
from `ui/` for frontend changes. Scripts such as `tests/live_smoke.py` require a
running model and are manual checks, not part of the default test suite.

Never include credentials, personal conversations, vault contents, local
configuration, logs, or desktop screenshots in commits or issue attachments.
Use synthetic data for examples and tests. Inspect `git diff --cached` before
committing. Report security issues privately as described in SECURITY.md.
