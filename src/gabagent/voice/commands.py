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
    kind: str          # "brain" | "undo" | "query" | "forget" | "quiet" | "floor"
    value: str = ""    # brain: local|cloud · floor: local|aria · query: model|where|recap|error|caps|memory · forget: last|all


# -- detection -------------------------------------------------------------

_VERB = r"(?:switch(?:\s+to)?|go(?:\s+to)?|back\s+to|move\s+to|change\s+to|use|launch|run|load)"
_TO_LOCAL = re.compile(rf"\b{_VERB}\s+(?:the\s+)?(?:local|devstral)\b", re.I)
# Cloud aliases include the persona name "Aria" and model "arya" (they sound alike; users say "Aria").
_TO_CLOUD = re.compile(
    rf"\b{_VERB}\s+(?:the\s+)?(?:cloud|online|claude|arya|aria|remote|gab)\b", re.I
)
_GO_ONLINE = re.compile(r"\bgo\s+(?:back\s+)?online\b", re.I)

_UNDO = re.compile(r"\b(?:undo|revert|roll\s*back|take\s+that\s+back)\b", re.I)

# "Shut up / be quiet" — answered with ONE short canned line, never the LLM. Telling the assistant to
# stop talking shouldn't draw a multi-sentence reply that itself keeps talking (and, while media plays,
# holds the duck down for the length of that reply — Rob's 2026-06-16 "shut up voice mode" → 3.4s
# paragraph that pinned the music ducked). The brain can't actually mute or sleep itself (the voice
# layer owns that), so the terse reply points at the sleep control. Kept strict — only talking-directed
# phrases — so it can NEVER swallow a media "stop"/"pause" (those have an object: "stop the music").
_QUIET = re.compile(
    r"\bshut\s*up\b"
    r"|\bshut\s+it\b"
    r"|\bbe\s+quiet\b"
    r"|\bquiet\s+down\b"
    r"|\bstop\s+(?:talking|yapping|yammering)\b"
    r"|\bhush\b"
    r"|\bpipe\s+down\b"
    r"|\bzip\s+it\b"
    r"|\bthat'?s\s+enough\b"
    r"|\benough\s+(?:talking|already)\b",
    re.I,
)

# Cross-backend ladder FLOOR toggle (distinct from the exclusive _TO_LOCAL/_TO_CLOUD switch above):
# make the local model the warm bottom rung (router still escalates Aria→Claude), or drop it so Aria
# is the floor. Kept strict — must name a floor/fallback/default qualifier — so "switch to local"
# (exclusive mode) and "use Aria" (exclusive cloud) keep their existing meaning.
_QUAL = r"(?:floor|fallback|bottom\s+rung|default|baseline)"
_FLOOR_OFF = re.compile(
    rf"\bdrop\s+(?:the\s+)?local\b"
    rf"|\b(?:turn\s+off|disable|unload|shut\s+down)\s+(?:the\s+)?local\b"
    rf"|\b(?:aria|arya)\b.{{0,25}}?\b{_QUAL}\b"
    rf"|\b{_QUAL}\b.{{0,25}}?\b(?:aria|arya)\b",
    re.I,
)
_FLOOR_ON = re.compile(
    rf"\blocal\b.{{0,25}}?\b{_QUAL}\b"
    rf"|\b{_QUAL}\b.{{0,25}}?\blocal\b",
    re.I,
)

# Deterministic "which model am I on" — answered from brain state, never the LLM (the local
# model can't reliably introspect its own runtime). Broadened to natural spoken variants.
_Q_MODEL = re.compile(
    r"\b(?:what|which)\b.*\b(?:model|brain)\b"
    r"|\blocal\s+or\s+cloud\b"
    r"|\bare\s+you\s+(?:running\s+|on\s+)?(?:a\s+|the\s+)?(?:local|cloud)\b"
    r"|\b(?:verify|check|confirm)\s+(?:your\s+|the\s+|which\s+)?(?:model|brain)\b"
    r"|\bwhere\s+are\s+you\s+running\b",
    re.I,
)
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
# "what went wrong" — answered from the stored last_error, NOT by re-running the failed action.
_Q_ERROR = re.compile(
    r"\bwhat(?:'?s| was| is)?\s+(?:the\s+|that\s+)?error\b"
    r"|\bwhat\s+went\s+wrong\b"
    r"|\bwhat\s+happened\b"
    r"|\bwhy\s+did\s+(?:that|it)\s+(?:fail|break|error|stop)\b",
    re.I,
)


# "what can you do" — list real capabilities from the catalog, not the model's guess.
_Q_CAPS = re.compile(
    r"\bwhat\s+can\s+you\s+(?:do|help|control)\b"
    r"|\bwhat\s+are\s+you\s+able\s+to\b"
    r"|\b(?:list|tell\s+me)\s+(?:your\s+)?capabilit",
    re.I,
)
# "what do you remember" — read from saved memory.
_Q_MEMORY = re.compile(
    r"\bwhat\s+do\s+you\s+(?:remember|know\s+about)\b"
    r"|\bwhat\s+have\s+you\s+saved\b"
    r"|\bwhat'?s?\s+in\s+your\s+memory\b",
    re.I,
)
# "forget …" — drop the last note, or wipe all on an explicit everything/all. Kept strict so
# casual speech ("forget about it, move on" / "clear the screen") doesn't wipe memory.
_FORGET = re.compile(
    r"\bforget\s+(?:that|it|everything|all(?:\s+of\s+it)?|the\s+last(?:\s+\w+)?|your\s+memory)\b"
    r"|\bclear\s+(?:your\s+)?memory\b",
    re.I,
)
_FORGET_ALL = re.compile(r"\b(?:everything|all(?:\s+of\s+it)?|your\s+memory|memory)\b", re.I)


def detect_meta_command(text: str) -> MetaCommand | None:
    t = text.strip()
    # Floor toggle is checked BEFORE the exclusive switch (it's the more specific, qualified phrase).
    if _FLOOR_OFF.search(t):
        return MetaCommand("floor", "aria")
    if _FLOOR_ON.search(t):
        return MetaCommand("floor", "local")
    if _TO_LOCAL.search(t):
        return MetaCommand("brain", "local")
    if _TO_CLOUD.search(t) or _GO_ONLINE.search(t):
        return MetaCommand("brain", "cloud")
    if _QUIET.search(t):
        return MetaCommand("quiet")
    if _UNDO.search(t):
        return MetaCommand("undo")
    if _Q_MODEL.search(t):
        return MetaCommand("query", "model")
    if _Q_WHERE.search(t):
        return MetaCommand("query", "where")
    if _Q_ERROR.search(t):
        return MetaCommand("query", "error")
    if _Q_CAPS.search(t):
        return MetaCommand("query", "caps")
    if _Q_MEMORY.search(t):
        return MetaCommand("query", "memory")
    if _FORGET.search(t):
        return MetaCommand("forget", "all" if _FORGET_ALL.search(t) else "last")
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
            keep_alive="1m",  # short idle unload — gabagent-scoped, not the global default
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
    """Return to the cloud brain (router re-enabled). Ollama is left running, but the
    local model is proactively unloaded so VRAM frees immediately."""
    ctx.local_mode = False
    ctx.active_model = None
    if ctx.voice_session is not None:
        ctx.voice_session.disarm_all()
    from gabagent.local.ollama import unload_local
    await unload_local(ctx)
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
        where = "the local model" if ctx.local_mode else "the cloud model"
        return f"I'm running {where} {current_brain(ctx)} right now."
    if which == "where":
        from gabagent.permissions.tiers import _default_zones
        zones = ", ".join(z.name or str(z) for z in _default_zones(ctx.cwd, ctx.config))
        return (
            f"I'm working in {ctx.cwd.name}. I can edit freely in {zones}; "
            f"changes to this project need a spoken okay, and risky actions need keyboard confirmation."
        )
    if which == "error":
        vs = getattr(ctx, "voice_session", None)
        err = getattr(vs, "last_error", None) if vs is not None else None
        return f"The last error was: {err}." if err else "Nothing's gone wrong recently."
    if which == "caps":
        return capabilities_brief(ctx)
    if which == "memory":
        return _memory_summary(ctx)
    if which == "recap":
        return _recap(ctx)
    return "I'm not sure what you're asking."


_DOMAIN_PHRASE = {
    "media": "play & search music and movies (TIDAL, Jellyfin) and control playback",
    "apps": "open apps and websites",
    "window": "manage windows — maximize, minimize, move between monitors, list screens",
    "desktop": "take screenshots, close tabs, and quit apps",
    "system": "adjust the volume and power",
}


def _join(items: list[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + ", and " + items[-1]


def capabilities_brief(ctx: AgentContext) -> str:
    cat = getattr(ctx, "command_catalog", None)
    domains = sorted({c.domain for c in cat.all()}) if cat is not None else []
    if not domains:
        return "Right now I can read and edit files, search the web, and run shell tasks for you."
    phrases = [_DOMAIN_PHRASE.get(d, d) for d in domains]
    return ("Besides reading and editing files and searching the web, I can " + _join(phrases)
            + ". Just ask.")


def _memory_summary(ctx: AgentContext) -> str:
    from gabagent.session.memory import MemoryManager
    text = MemoryManager(ctx.cwd).load().strip()
    if not text:
        return "I haven't saved anything about this project yet."
    blocks = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("## ")]
    n = text.count("\n## ") + (1 if text.startswith("## ") else 0)
    last = blocks[-1].strip() if blocks else ""
    if last:
        return f"I've got {n} note{'s' if n != 1 else ''} saved. The most recent: {last}"
    return f"I've got {n} note{'s' if n != 1 else ''} saved about this project."


def forget(ctx: AgentContext, scope: str) -> str:
    from gabagent.session.memory import MemoryManager
    mgr = MemoryManager(ctx.cwd)
    if scope == "all":
        mgr.clear()
        return "Okay, I've cleared everything I'd saved about this project."
    return ("Okay, I've forgotten the last thing I noted." if mgr.pop_last()
            else "There's nothing saved to forget.")


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
