"""Deterministic, no-LLM voice meta-commands: brain switching, undo, status queries.

`detect_meta_command(text)` recognizes a tight set of spoken control phrases so they
never go to the model (fast, no dead air, no hallucination). Matching is intentionally
strict (explicit verb + target) to avoid false positives like "open the local file".
"""
from __future__ import annotations
import random
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


@dataclass
class MetaCommand:
    kind: str          # "brain" | "undo" | "query"
    value: str = ""    # brain: local|cloud  ·  query: model|where|recap


# -- detection -------------------------------------------------------------

_VERB = r"(?:switch(?:\s+to)?|go(?:\s+to)?|back\s+to|move\s+to|change\s+to|use|launch|run|load)"
_TO_LOCAL = re.compile(rf"\b{_VERB}\s+(?:the\s+)?local\b", re.I)
_TO_CLOUD = re.compile(rf"\b{_VERB}\s+(?:the\s+)?(?:cloud|online|claude|arya|remote|gab)\b", re.I)
_GO_ONLINE = re.compile(r"\bgo\s+(?:back\s+)?online\b", re.I)

_UNDO = re.compile(r"\b(?:undo|revert|roll\s*back|take\s+that\s+back)\b", re.I)

_Q_MODEL = re.compile(r"\b(?:what|which)\b.*\b(?:model|brain)\b|\blocal\s+or\s+cloud\b", re.I)
_Q_WHERE = re.compile(
    r"\b(?:what|which)\b.*\b(?:folder|directory|dir)\b"
    r"|\bwhat\s+can\s+you\s+(?:touch|access|edit|write)\b"
    r"|\bwhere\s+are\s+you\s+working\b"
    r"|\bsafe\s+(?:zones?|folders?)\b",
    re.I,
)
_Q_RECAP = re.compile(
    r"\b(?:recap|what\s+have\s+you\s+done|what\s+did\s+you\s+do)\b", re.I
)


def detect_meta_command(text: str) -> MetaCommand | None:
    t = text.strip()
    if _TO_LOCAL.search(t):
        return MetaCommand("brain", "local")
    if _TO_CLOUD.search(t) or _GO_ONLINE.search(t):
        return MetaCommand("brain", "cloud")
    if _UNDO.search(t):
        return MetaCommand("undo")
    if _Q_MODEL.search(t):
        return MetaCommand("query", "model")
    if _Q_WHERE.search(t):
        return MetaCommand("query", "where")
    if _Q_RECAP.search(t):
        return MetaCommand("query", "recap")
    return None


# -- persona-keyed filler phrases ------------------------------------------

_FILLERS: dict[str, dict[str, list[str]]] = {
    "to_local": {
        "": ["Local model request confirmed, switching now.", "Going local, one moment."],
        "butler": ["Very good. Engaging the local model now.", "Switching to the local model, sir."],
        "cheerful": ["You got it — hopping over to the local model!", "Local it is, switching now!"],
        "deadpan": ["Switching to local.", "Local model. Done."],
    },
    "to_cloud": {
        "": ["Back to the cloud, switching now.", "Going online, one moment."],
        "butler": ["Returning to the cloud, sir.", "Re-engaging the cloud brain now."],
        "cheerful": ["Back to the cloud we go!", "Cloud brain, coming right up!"],
        "deadpan": ["Cloud. Switching.", "Back online."],
    },
    "escalate": {
        "": ["Switching to Claude, one moment.", "This one needs Claude, hang on."],
        "butler": ["A moment — consulting Claude for this.", "Engaging Claude, sir."],
        "cheerful": ["Ooh, calling in Claude for this one!", "Bringing in Claude, hang tight!"],
        "deadpan": ["Escalating to Claude.", "Claude now."],
    },
    "code": {
        "": ["I've drafted some code.", "Code's written out."],
        "butler": ["The code has been prepared, sir."],
        "cheerful": ["Code's all written out for you!"],
        "deadpan": ["Code written."],
    },
}


def filler(category: str, ctx: AgentContext) -> str:
    persona = (getattr(ctx.config, "voice_persona", "") or "").strip().lower()
    pool = _FILLERS.get(category, {})
    choices = pool.get(persona) or pool.get("", ["One moment."])
    return random.choice(choices)


# -- handlers --------------------------------------------------------------

async def switch_to_local(ctx: AgentContext) -> str | None:
    """Start/attach the local Ollama model. Returns None on success, else an error string."""
    if not ctx.config.local_model:
        return "no local model is configured"
    from gabagent.local.ollama import ensure_ollama_running
    from gabagent.api.client import GabAIClient
    from gabagent.api.models import ChatMessage

    err = await ensure_ollama_running(ctx)
    if err:
        return err
    if ctx.local_client is None:
        ctx.local_client = GabAIClient(
            api_key="ollama",
            base_url=ctx.config.local_base_url,
            model=ctx.config.local_model,
            rate_limiter=ctx.rate_limiter,
        )
    # Compact briefing so the local model has context without the full history.
    messages = ctx.session.messages()
    user_assistant = [m for m in messages if m.role in ("user", "assistant")]
    if user_assistant:
        summary_prompt = [
            ChatMessage(role="system", content="You are a concise technical summarizer."),
            *user_assistant[-20:],
            ChatMessage(role="user", content=(
                "Summarize this conversation into a compact spoken briefing (under 200 words) "
                "for a local assistant that will continue the work. Be specific about the "
                "current task and any files involved."
            )),
        ]
        try:
            ctx.local_context_summary = await ctx.client.complete_simple(summary_prompt)
        except Exception:
            ctx.local_context_summary = None
    ctx.local_mode = True
    if ctx.voice_session is not None:
        ctx.voice_session.disarm_all()
    return None


async def switch_to_cloud(ctx: AgentContext) -> str | None:
    """Return to the cloud brain (router re-enabled). Ollama is left running."""
    ctx.local_mode = False
    ctx.active_model = None
    if ctx.voice_session is not None:
        ctx.voice_session.disarm_all()
    return None


def undo_last(ctx: AgentContext) -> str:
    """Revert the most recent voice-made file write. Returns a spoken result."""
    from pathlib import Path
    vs = ctx.voice_session
    item = vs.pop_undo() if vs is not None else None
    if item is None:
        return "There's nothing to undo."
    path, prior = item
    p = Path(path)
    try:
        if prior is None:
            if p.exists():
                p.unlink()
            return f"Reverted — removed {p.name}."
        p.write_bytes(prior)
        return f"Reverted {p.name}."
    except Exception as e:
        return f"I couldn't undo that: {e}"


def current_brain(ctx: AgentContext) -> str:
    if ctx.local_mode:
        return ctx.config.local_model or "the local model"
    if ctx.force_model:
        return ctx.config.model
    return ctx.active_model or ctx.config.router.simple_model


def answer_query(ctx: AgentContext, which: str) -> str:
    if which == "model":
        return f"I'm using {current_brain(ctx)} right now."
    if which == "where":
        from gabagent.permissions.tiers import _default_zones
        zones = ", ".join(z.name or str(z) for z in _default_zones(ctx.cwd, ctx.config))
        return (
            f"I'm working in {ctx.cwd.name}. I can edit freely in {zones}; "
            f"changes to this project need a spoken okay, and risky actions need keyboard confirmation."
        )
    if which == "recap":
        return _recap(ctx)
    return "I'm not sure what you're asking."


def _recap(ctx: AgentContext, n: int = 5) -> str:
    import json
    from pathlib import Path
    path = getattr(ctx, "voice_audit_path", None)
    if not path or not Path(path).exists():
        return "I haven't done anything that needed confirming yet."
    try:
        lines = Path(path).read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        return "I couldn't read my activity log."
    entries = []
    for line in lines[-n:]:
        try:
            e = json.loads(line)
            entries.append(f"{e.get('decision', '?')} {e.get('summary', e.get('tool', 'an action'))}")
        except Exception:
            continue
    if not entries:
        return "I haven't done anything that needed confirming yet."
    return "Recently: " + "; ".join(entries) + "."
