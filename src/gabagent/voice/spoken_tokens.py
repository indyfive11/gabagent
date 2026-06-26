"""Spoken → token assembly: turn faithfully-transcribed speech into real path / identifier tokens.

VAC keeps STT honest — it transcribes "slash tmp slash builder dash test" and "hello dot txt"
verbatim rather than guessing punctuation. The BRAIN is the deliberate owner that assembles those
spoken words into actual tokens (`/tmp/builder-test`, `hello.txt`) when parsing a file or builder
command (Rob's call, 2026-06-24 — brain owns assembly; the voice-side normalizer is fallback only).

Deterministic and BOUNDED — applied only at a path/name boundary (builder VUI args, builder/file path
args), never to free conversation:
  - `normalize_name` converts only INFIX separators ("snake dash game" → "snake-game") so a project
    literally named "dash" is left intact.
  - `assemble_path` is full (handles a leading "slash" → "/"), but reached only via
    `maybe_assemble_path`, gated on `looks_spoken` — a clean path never contains the WORD "slash", so
    "/tmp/x.py" passes through untouched.
"""
from __future__ import annotations

import re

# Spoken symbol words → symbol, for full PATH assembly. Multi-word forms first.
_PATH_SYMBOLS: list[tuple[str, str]] = [
    (r"forward\s+slash", "/"),
    (r"back\s*slash", "\\"),
    (r"slash", "/"),
    (r"dot", "."),
    (r"hyphen", "-"),
    (r"dash", "-"),
    (r"underscore", "_"),
    (r"tilde", "~"),
]

# Separators allowed INSIDE an identifier name (infix only — see module docstring).
_NAME_SEPS: list[tuple[str, str]] = [
    (r"hyphen", "-"), (r"dash", "-"), (r"underscore", "_"), (r"dot", "."),
]

# Spoken / homophone number words → digit, for a TRAILING sequel number on a media title
# ("Pitch Perfect to" → "Pitch Perfect 2"). Conservative core agreed with VAC — only reliable homophones;
# deliberately EXCLUDES collision-prone ones (free→3, sicks→6, sven→7). The CALLER guards false positives
# by applying the digit only when a "<title> N" candidate actually exists in the resolved library.
_TRAILING_NUMBER: dict[str, str] = {
    "two": "2", "to": "2", "too": "2",
    "three": "3", "tree": "3",
    "four": "4", "for": "4", "fore": "4",
    "eight": "8", "ate": "8",
    "one": "1", "won": "1",
    "nine": "9",
    "ten": "10",
}


def normalize_trailing_number(text: str) -> str:
    """Map a TRAILING spoken/homophone number word to its digit: 'Pitch Perfect to' → 'Pitch Perfect 2'.
    Only the LAST token, and only when ≥1 word precedes it (so a bare 'two' or 'for' alone is left intact).
    Returns the input unchanged when nothing applies — the caller decides whether the digit form matches a
    real library title (the false-positive guard). Never raises."""
    s = (text or "").strip()
    toks = s.split()
    if len(toks) < 2:
        return s
    last = toks[-1].lower().strip(".,!?;:")
    digit = _TRAILING_NUMBER.get(last)
    if not digit:
        return s
    return " ".join(toks[:-1] + [digit])


def looks_spoken(text: str) -> bool:
    """True when the string carries a spoken-path marker — the safe gate for path assembly. A clean
    filesystem path never contains the WORD 'slash'/'backslash' as a token."""
    return bool(re.search(r"\b(?:forward\s+slash|back\s*slash|slash)\b", text or "", re.I))


def assemble_path(text: str) -> str:
    """'slash tmp slash builder dash test' → '/tmp/builder-test'; 'hello dot txt' → 'hello.txt'.
    Collapses internal whitespace (an assembled path carries none)."""
    s = (text or "").strip()
    for pat, sym in _PATH_SYMBOLS:
        s = re.sub(rf"\s*\b{pat}\b\s*", lambda _m, _s=sym: _s, s, flags=re.I)
    return re.sub(r"\s+", "", s)


def maybe_assemble_path(text: str) -> str:
    """Assemble only when the text looks spoken; otherwise return it unchanged (clean paths untouched)."""
    return assemble_path(text) if looks_spoken(text) else (text or "")


def normalize_name(text: str) -> str:
    """Spoken identifier → a clean name ready to slugify: 'snake dash game' → 'snake-game'. Only
    converts a separator word sitting BETWEEN two word characters, so a bare 'dash'/'dot' name survives."""
    s = (text or "").strip()
    for word, sym in _NAME_SEPS:
        s = re.sub(rf"(?<=\w)\s+\b{word}\b\s+(?=\w)", sym, s, flags=re.I)
    return s.strip()
