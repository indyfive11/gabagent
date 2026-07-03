"""Loads Aria's self-knowledge doc — the curated "how I work" notes injected on introspective turns.

The PACKAGED about_aria.md is the source of truth, so updates ship with the code (and stay accurate as
the system changes — deliberately UNLIKE the persona seed, which copies to disk once and can go stale).
A user may override it by creating <data_dir>/selfknowledge/about.md; that override is read only when
present, and is size-capped so a large user file can't blow the per-turn prompt budget.
"""
from __future__ import annotations

import re
from pathlib import Path

from gabagent.config.paths import data_dir

_PACKAGED = Path(__file__).with_name("about_aria.md")
# Cap the override; mirrors the TMI Tier-1 budget so per-turn prompt cost stays bounded.
_OVERRIDE_CAP = 1500
# HTML comments are authoring guidance (for whoever edits the doc), never meant for the model — strip them.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _clean(text: str) -> str:
    return _COMMENT_RE.sub("", text).strip()


def _override_path() -> Path:
    """User override location (XDG-correct via data_dir; NOT created here — read only if it exists)."""
    return data_dir() / "selfknowledge" / "about.md"


def about_text() -> str:
    """The self-knowledge text: user override if present (capped), else the packaged doc. '' on error.
    HTML comments are stripped — only the actual knowledge reaches the prompt."""
    ov = _override_path()
    try:
        if ov.exists():
            t = _clean(ov.read_text(encoding="utf-8"))
            if t:
                return t[:_OVERRIDE_CAP].rstrip()
    except Exception:
        pass
    try:
        return _clean(_PACKAGED.read_text(encoding="utf-8"))
    except Exception:
        return ""
