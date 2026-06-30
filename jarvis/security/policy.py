"""Autonomy policy: path scope and catastrophic-op tripwires.

Full autonomy means no routine confirmations, but a small allowlist of
catastrophic operations trips a flag the agent surfaces (PLANv3 / PLANv2 §13).
This is capability/visibility control in code, not prompt instructions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TripwireHit:
    name: str
    reason: str


# Path fragments that should never be auto-modified.
_PROTECTED = [
    re.compile(r"(?i)^[a-z]:\\windows\\"),
    re.compile(r"(?i)^[a-z]:\\program files"),
    re.compile(r"(?i)\\system32\\"),
    re.compile(r"(?i)/etc/"),
    re.compile(r"(?i)/system/"),
]

_CATASTROPHIC_CMDS = [
    re.compile(r"(?i)\bformat\b\s+[a-z]:"),
    re.compile(r"(?i)\bRemove-Item\b.*\b-Recurse\b.*\b-Force\b.*\\"),
    re.compile(r"(?i)\brm\s+-rf\s+/"),
    re.compile(r"(?i)\breg(\.exe)?\s+delete\b.*HK"),
    re.compile(r"(?i)\b(diskpart|mkfs|fdisk)\b"),
]


class Policy:
    def __init__(self, delete_threshold: int = 50, tripwires_enabled: bool = True) -> None:
        self.delete_threshold = delete_threshold
        self.tripwires_enabled = tripwires_enabled

    def is_protected_path(self, path: str) -> bool:
        return any(p.search(path) for p in _PROTECTED)

    def check_shell(self, command: str) -> TripwireHit | None:
        if not self.tripwires_enabled:
            return None
        for pat in _CATASTROPHIC_CMDS:
            if pat.search(command):
                return TripwireHit("catastrophic_command", f"command matches {pat.pattern}")
        return None

    def check_bulk_delete(self, count: int) -> TripwireHit | None:
        if not self.tripwires_enabled:
            return None
        if count >= self.delete_threshold:
            return TripwireHit("bulk_delete", f"{count} items >= threshold {self.delete_threshold}")
        return None

    # -- agentic guards: things that should pause for confirmation ---------- #
    def secret_in_text(self, text: str) -> bool:
        """True if `text` looks like a credential/API key/private key being typed."""
        return bool(text) and any(p.search(text) for p in _SECRET_PATTERNS)

    def foreground_sensitive(self) -> str | None:
        """Window-title inspection is disabled; monitor state must go through vision."""
        return None


# Secret-looking strings (typed values) and sensitive-window heuristics.
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),                      # OpenAI-style
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),               # GitHub tokens
    re.compile(r"AKIA[0-9A-Z]{12,}"),                        # AWS access key id
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),                  # Google API key
    re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,}"),            # Slack tokens
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),       # PEM private keys
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
    re.compile(r"\b[0-9a-fA-F]{40,}\b"),                     # long hex secret
]
_SENSITIVE_SURFACE = re.compile(
    r"(password|passcode|sign[\s\-]?in|log[\s\-]?in|credential|credit card|card number|"
    r"\bcvv\b|\bcvc\b|billing|checkout|payment|\bbank\b|2fa|one[\s\-]?time code|authenticator)",
    re.I,
)
