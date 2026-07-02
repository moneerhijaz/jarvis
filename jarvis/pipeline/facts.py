"""PLANv5 §6/decision #2 — compare facts, not format.

Extract the *facts* from an answer (file paths, numbers + units, number-words) into a
normalized set, so "four" == "4" and `C:\\a\\b` == `c:/a/b`. Used by single-verify
(does the answer's facts come from the evidence?) and by double-verify (do the two
responses' facts agree?). Deterministic; no model.
"""
from __future__ import annotations

import re

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "thousand": 1000,
    "million": 1_000_000, "billion": 1_000_000_000,
}
# Windows drive paths (backslash OR forward slash — "C:\a\b" and "c:/a/b" are the same path),
# or unix paths with at least two segments (so "/no_think" or "and/or" aren't mistaken for paths).
_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|]+|(?:/[^\s\"'<>|/]+){2,}")
_THINK_RE = re.compile(r"(?is)<think>.*?</think>")
_SCAFFOLD = ("thinking process", "analyze the request", "determine the answer",
             "format the output", "final check", "draft:", "final polish", "refine the output")
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WORD_RE = re.compile(r"[a-z]+")


def _norm_num(s: str) -> str:
    s = s.replace(",", "")
    try:
        f = float(s)
        return str(int(f)) if f.is_integer() else str(round(f, 2))
    except ValueError:
        return s


def _norm_path(p: str) -> str:
    return p.rstrip(".,);]").replace("\\", "").replace("/", "").lower()


def strip_reasoning(text: str) -> str:
    """Remove leaked chain-of-thought so a thinking model's answer can be compared/spoken.
    The reliable fix is disabling thinking in LM Studio; this is defense-in-depth."""
    t = _THINK_RE.sub("", text or "")
    t = re.sub(r"\s*/no_?think\s*", " ", t, flags=re.I)
    low = t.lower()
    if any(s in low for s in _SCAFFOLD):
        # the model dumped its steps; keep the last short, answer-like line if any
        lines = [ln.strip(" -*#\t") for ln in t.splitlines() if ln.strip()]
        for ln in reversed(lines):
            l = ln.lower()
            if len(ln) <= 200 and not any(l.startswith(s.split()[0]) for s in _SCAFFOLD) \
                    and not re.match(r"^\d+\.", ln):
                return ln.strip()
        return ""        # only reasoning, nothing answer-like -> unsalvageable (caller degrades)
    return t.strip()


def extract_facts(text: str) -> set[str]:
    """Normalized fact set: ``path:<...>`` and ``num:<...>`` tokens."""
    t = text or ""
    facts: set[str] = set()
    for p in _PATH_RE.findall(t):
        facts.add("path:" + _norm_path(p))
    for n in _NUM_RE.findall(t):
        facts.add("num:" + _norm_num(n))
    # number-words -> digits, so "four" matches "4"
    for w in _WORD_RE.findall(t.lower()):
        if w in _UNITS:
            facts.add("num:" + str(_UNITS[w]))
    return facts


_STOP = {"the", "a", "an", "is", "are", "was", "were", "of", "to", "in", "on", "it", "its",
         "and", "or", "that", "this", "for", "with", "as", "be", "your", "you", "i", "equals",
         "equal", "answer", "result", "plus", "minus", "times", "by"}


def _norm_text(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _key_terms(text: str) -> set[str]:
    return {w for w in _norm_text(text).split() if w not in _STOP}


def facts_agree(a: str, b: str) -> tuple[bool, dict]:
    """Do two responses agree? Uses CONTAINMENT, not equality, so a verbose answer that adds
    correct context ("two plus two equals four") still agrees with a terse one ("4"): they
    agree as long as neither CONTRADICTS the other. Numbers/paths first; else key-term tokens."""
    fa, fb = extract_facts(a), extract_facts(b)
    if fa and fb:
        agree = fa <= fb or fb <= fa     # one set contained in the other = no contradiction
        return agree, {"mode": "facts", "only_in_a": sorted(fa - fb), "only_in_b": sorted(fb - fa),
                       "agree": agree}
    # If only ONE side has extractable facts, the empty set would be "contained" in anything and
    # the check would auto-pass — a factless pass must NOT vouch for a factful one. Fall through
    # to the key-term comparison instead.
    ta, tb = _key_terms(a), _key_terms(b)
    if not ta or not tb:
        agree = _norm_text(a) == _norm_text(b)
        return agree, {"mode": "text", "agree": agree}
    agree = ta <= tb or tb <= ta         # key-term containment for objective short answers
    return agree, {"mode": "text-contains", "only_in_a": sorted(ta - tb)[:8],
                   "only_in_b": sorted(tb - ta)[:8], "agree": agree}


def ungrounded_facts(answer: str, evidence: str) -> list[str]:
    """Facts asserted in the answer that don't appear in the evidence (decision #2 grounding).
    Numbers are noisy (counts, indices), so we only police PATHS here — the common hallucination."""
    ev = extract_facts(evidence)
    bad = [f for f in extract_facts(answer) if f.startswith("path:") and f not in ev]
    return bad
