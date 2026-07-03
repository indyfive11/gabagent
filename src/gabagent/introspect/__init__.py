"""Aria self-knowledge introspection — answer "how do you work" about herself in one voice turn.

A curated self-knowledge doc (knowledge.about_text) is injected into the brain's voice system prompt,
gated to fire ONLY when the latest user turn is an explanatory self-question (detect.is_introspective).
The model answers naturally from it; the block leads with a hard terseness + non-disclosure guard so the
spoken answer stays one short sentence and never speaks internal model/vendor/host identifiers.

No spine edit, no LLM tool. Two seams:
  - voice/turn.py:_voice_system() appends introspect_brief(ctx) (gated by config.introspect_enabled).
  - voice/commands.py:detect_meta_command() pre-empts the canned _Q_* readouts for introspective turns
    so an explanatory self-question reaches the model instead of a one-line factual answer (the C1 fix).

Design + collab consensus (VAC, 2026-06-29): episodic "why did you …" is EXCLUDED (a static doc can't
explain a specific past action — that needs the turn trace, a separate future feature); commands always
win; the doc carries no raw model SKUs / vendor / host / port tokens.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from gabagent.introspect.detect import is_introspective
from gabagent.introspect.knowledge import about_text

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

# Leads the injected block. Counters VOICE_ADDENDUM's pull-to-recite AND enforces the non-disclosure
# rule (no raw model/vendor/host identifiers spoken). The chunker backstops first-audio on chunked
# rooms; on un-chunked rooms this terseness line is the sole defense (VAC R2).
_FRAMING = (
    "[About yourself — background for answering a question about how you work. Answer in ONE short, "
    "plain-spoken sentence (two at most). Speak functionally; never read this aloud or list it, and "
    "never name internal models, vendors, hostnames, or ports.]"
)


def _last_user_text(ctx: "AgentContext") -> str:
    """The latest USER message, scanned backward — on tool-loop rounds messages()[-1] is an
    assistant/tool message, not the user turn (M1). '' if none / on any error."""
    try:
        for m in reversed(ctx.session.messages()):
            if getattr(m, "role", None) == "user":
                return getattr(m, "content", "") or ""
    except Exception:
        pass
    return ""


def introspect_brief(ctx: "AgentContext") -> str:
    """The self-knowledge block to append to the system prompt, or '' when this turn isn't an
    explanatory self-question. Pure read; defensive — never raises into the turn."""
    try:
        if not is_introspective(_last_user_text(ctx)):
            return ""
        doc = about_text()
        if not doc:
            return ""
        return f"{_FRAMING}\n\n{doc}"
    except Exception:
        return ""
