"""Introspective-intent detection for the self-knowledge layer.

is_introspective(text) is True for EXPLANATORY self-questions the model should answer from the injected
self-knowledge doc ("how do you decide which model", "what are your limits", "what happens if the
internet drops"). It is deliberately DISJOINT from the instant canned queries in voice/commands.py
(_Q_MODEL/_Q_CAPS/_Q_MEMORY answer short factual self-state with no model call) — the pre-empt in
detect_meta_command lets an introspective question fall THROUGH those to the model.

Two hard exclusions (VAC consensus 2026-06-29):
  - Episodic "why did you …" — a static doc cannot explain a specific past action (needs the turn
    trace); answering it generically is worse than not firing. Excluded → normal turn (today's behavior).
  - Device/media CONTROL commands ("how do you play jazz", "how do you turn on the lights") — commands
    always win, via the looks_like_command veto.
"""
from __future__ import annotations

import re

# "why did you …" — episodic; cannot be answered from a static self-knowledge doc. Checked first.
_EPISODIC_WHY = re.compile(r"\bwhy\s+did\s+you\b", re.I)

# Explanatory self-questions about identity / design / behavior. Anchored on second-person self-
# reference; NOT any "how do you X" (that would catch control phrasings — those are vetoed separately).
_SELF_DESIGN_RE = re.compile(
    # how you operate / reason / choose
    r"\bhow\s+(?:do|does|did)\s+you\s+(?:work|think|decide|choose|pick|reason|operate|function|run\b|figure)\b"
    # how you perceive
    r"|\bhow\s+(?:do|does|did)\s+you\s+(?:hear|listen|understand|know\s+(?:it'?s|who))\b"
    # how you were built
    r"|\bhow\s+(?:were|are|was)\s+you\s+(?:made|built|created|designed|trained|set\s+up)\b"
    # memory
    r"|\bhow\s+do\s+you\s+(?:remember|recall)\b|\bhow'?s\s+your\s+memory\b"
    r"|\bdo\s+you\s+remember\s+(?:things|stuff|me|anything)\b"
    # limits (inverse of capabilities — must not be swallowed by _Q_CAPS)
    r"|\bwhat\s+are\s+your\s+(?:limits|limitations|constraints|boundaries)\b"
    r"|\bwhat\s+can'?t\s+you\s+do\b|\bwhat\s+are\s+you\s+unable\b|\bwhat\s+don'?t\s+you\s+do\b"
    # identity
    r"|\btell\s+me\s+about\s+yourself\b|\bwho\s+are\s+you\b"
    r"|\bwhat\s+kind\s+of\s+(?:assistant|a\.?i\.?|ai|thing)\s+are\s+you\b"
    # connectivity / offline — the failover answer is a primary point of this feature
    r"|\bdo\s+you\s+need\s+(?:the\s+)?(?:internet|wi-?fi|a\s+connection|network)\b"
    r"|\bdo\s+you\s+work\s+offline\b"
    r"|\bwhat\s+happens?\s+(?:when|if)\b.{0,40}\b(?:internet|offline|wi-?fi|connection|network)\b"
    # general (habitual/policy) "why do you" — NOT episodic "why did you" (excluded above)
    r"|\bwhy\s+do\s+you\b",
    re.I,
)


def is_introspective(text: str) -> bool:
    """True when the utterance is an explanatory self-question to answer from the self-knowledge doc.
    Excludes episodic 'why did you …' and any device/media control command."""
    t = (text or "").strip()
    if not t:
        return False
    if _EPISODIC_WHY.search(t):
        return False
    try:
        from gabagent.agent.router import looks_like_command
        if looks_like_command(t):
            return False
    except Exception:
        pass
    return bool(_SELF_DESIGN_RE.search(t))
