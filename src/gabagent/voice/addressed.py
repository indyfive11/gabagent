"""'Addressed-to-me?' intent filter for voice turns.

A wake/command window stays open ~15s so the user can give multi-part commands; undirected speech
leaks into it — frustration ("damn it"), thinking aloud, talking to someone else, or commentary
*about* the assistant ("the goal is the voice doesn't trip when I'm just talking"). A false wake
opens such a window too. The voice side can't tell intent from window state, so the brain decides
here whether an utterance is actually directed at the assistant; if not, the turn emits nothing.

CONSERVATIVE BY DESIGN: when unsure it returns addressed=True. The cost of occasionally answering an
aside is far lower than dropping a real command, so the filter must never eat a command — every
ambiguity resolves toward answering.

HYBRID (per Rob, 2026-06-04): a fast heuristic passes obvious commands/questions through with zero
latency; only utterances that don't obviously look like a request pay for a one-shot LLM classify.
The heuristic NEVER returns "not addressed" on its own — it only short-circuits the clearly-addressed
case, leaving every genuine judgment call to the model with an answer-when-unsure prompt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gabagent.api.models import ChatMessage

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

# Leading tokens that mark an utterance as a clear request/command/question to the assistant. First
# word only (after stripping filler), so an aside that merely *contains* one of these isn't caught —
# that nuance is left to the model.
_COMMAND_LEADS = frozenset((
    # media / device control
    "play", "pause", "stop", "resume", "skip", "next", "previous", "restart", "turn", "set",
    "volume", "mute", "unmute", "louder", "quieter", "lower", "raise", "open", "close", "launch",
    "start", "switch", "undo", "redo",
    # info / agent requests
    "show", "find", "search", "list", "read", "tell", "give", "make", "create", "write", "edit",
    "run", "add", "remove", "delete", "move", "copy", "send", "put", "get", "check", "look",
    "go", "take", "bring", "call", "ask", "help",
    # question words → a question is addressed
    "what", "what's", "whats", "where", "where's", "when", "who", "who's", "why", "how", "how's",
    "is", "are", "can", "could", "would", "will", "should", "do", "does", "did", "may",
))

# Filler/politeness to strip before reading the lead word, so "hey, play X" / "okay turn it up" /
# "please pause" still fast-path. ("hey aria" handled separately — naming the assistant is addressed.)
_FILLER_LEADS = frozenset((
    "hey", "ok", "okay", "uh", "um", "well", "so", "yeah", "yes", "no", "please", "now", "just",
    "alright", "hi", "hello",
))

_ADDRESSED_PROMPT = """\
You decide whether the user is speaking TO a voice assistant named Aria, or NOT.
Aria's wake-word window is open, so speech NOT meant for her can leak in.

Return ONLY one tag — no other text:
[ADDRESSED] — a request, question, or command for Aria to act on or answer (including indirect ones
like "it's too quiet" or "I can't hear it").
[ASIDE] — NOT for Aria: an exclamation or curse, thinking aloud, talking to someone else, or
commentary ABOUT Aria rather than a request to her.

Decisive rule: speech that REFERS TO Aria but is aimed at someone else is an ASIDE, even when it uses
"you" or sounds like a question. Addressed means the user wants Aria herself to respond NOW.
Examples:
- "that was a false positive" → [ASIDE] (commentary about her)
- "the goal is it doesn't trip when I'm just talking" → [ASIDE]
- "he still hasn't bothered to respond, if you noticed" → [ASIDE] (narrating to someone else, "you" is about her)
- "isn't that incredible?" → [ASIDE] (rhetorical, not a request)
- "Mel, tell me something interesting" → [ASIDE] (addressed to Mel)
- "turn it up" / "what's the weather" / "it's too quiet in here" → [ADDRESSED]

When genuinely unsure, return [ADDRESSED].

Utterance: {text}"""


def _fast_verdict(text: str) -> bool | None:
    """Heuristic short-circuit. Returns True only for an *obviously addressed* utterance (a question,
    or one led by a command/question word). Returns None for everything else → defer to the model.
    NEVER returns False: the heuristic alone can never suppress a turn."""
    t = text.strip().lower()
    if not t:
        return None
    # NB: a trailing "?" is deliberately NOT a fast-pass — rhetoricals ("isn't that wild?", "really?")
    # are asides, so anything question-shaped but not led by a question word goes to the classifier.
    # Genuine questions still fast-pass via their leading question word (what/how/is/are/can…) below.
    words = [w.strip(".,!?;:\"'") for w in t.split()]
    if "aria" in words:                         # naming the assistant is addressed
        return True
    # Skip leading filler/politeness to read the real lead word.
    i = 0
    while i < len(words) and words[i] in _FILLER_LEADS:
        i += 1
    if i < len(words) and words[i] in _COMMAND_LEADS:
        return True
    return None


async def is_addressed(ctx: AgentContext, user_text: str) -> tuple[bool, str]:
    """Decide whether `user_text` is directed at the assistant. Returns (addressed, via) where `via`
    is one of 'fast' | 'llm:addressed' | 'llm:aside' | 'error'. Never raises; fails open (addressed)
    so a classifier hiccup can never drop a command."""
    fast = _fast_verdict(user_text)
    if fast:
        return True, "fast"
    try:
        from gabagent.agent.loop import _active_client
        model = ctx.config.router.simple_model
        messages = [ChatMessage(role="user", content=_ADDRESSED_PROMPT.format(text=user_text.strip()))]
        tag = await _active_client(ctx).complete_simple(messages, model=model)
        if "[ASIDE]" in tag.upper() and "[ADDRESSED]" not in tag.upper():
            return False, "llm:aside"
        return True, "llm:addressed"
    except Exception:
        return True, "error"
