"""Secret redaction for logs, audit entries, and vault writes.

Never log or persist secret-shaped values. Used by the tool executor (audit),
the diagnostics export, and the vault writer.
"""
from __future__ import annotations

import re
from typing import Any

_PLACEHOLDER = "[REDACTED]"

# Patterns are intentionally broad; false positives are safer than leaks.
_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?i)\b(sk|rk|pk)-[A-Za-z0-9]{16,}\b"),          # OpenAI-style keys
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]+"),               # bearer tokens
    re.compile(r"(?i)\bghp_[A-Za-z0-9]{20,}\b"),                  # GitHub tokens
    re.compile(r"(?i)\bxox[baprs]-[A-Za-z0-9\-]{10,}\b"),         # Slack tokens
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|authorization)\s*[:=]\s*['\"]?([^\s'\"]{6,})"),
    re.compile(r"\b(?:\d[ -]*?){13,16}\b"),                       # card-like numbers
    re.compile(r"(?i)\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),  # JWT
]

_KEY_HINTS = re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|authorization|cookie)")


def redact(text: str | None) -> str | None:
    if not text:
        return text
    out = text
    for pat in _PATTERNS:
        if pat.groups >= 2:
            out = pat.sub(lambda m: m.group(0).replace(m.group(2), _PLACEHOLDER), out)
        else:
            out = pat.sub(_PLACEHOLDER, out)
    return out


def redact_obj(obj: Any) -> Any:
    """Recursively redact dict/list/str structures (e.g. tool arguments)."""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and _KEY_HINTS.search(k) and isinstance(v, str):
                out[k] = _PLACEHOLDER
            else:
                out[k] = redact_obj(v)
        return out
    if isinstance(obj, list):
        return [redact_obj(x) for x in obj]
    return obj
