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
    # B2 (data-mined from real deferred-addressed turns 2026-06-22): common media verbs the gate was
    # paying an LLM classify on — "push play", "watch the movie", "shuffle the playlist", "jump to 45
    # minutes", "rewind". All imperative media controls; a rare aside match only costs one LLM call.
    "push", "watch", "shuffle", "jump", "rewind",
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
    # B2: strip a leading "let's"/"lets" so the COMMAND after it is read ("let's play X" → play). The
    # opener comment always intended this but the token was missing. Safe: "let's see/think" (thinking
    # aloud) doesn't match — the following word isn't a command lead, so it still defers to the LLM.
    "let's", "lets",
))

# The assistant's name, for a TRAILING vocative fast-pass ("…, Arya?"). STT spells it both ways. A
# preposition right before the name signals narration ABOUT her ("talking TO aria", "with aria"), not
# address — those defer to the classifier rather than fast-passing.
_NAME_TOKENS = frozenset(("aria", "arya"))
_NARRATION_PRE = frozenset(("to", "with", "about", "and", "or", "tell", "told"))

# Greeting tokens (a subset is also filler). Used two ways: (1) a trailing "<greeting> aria" is a WAKE
# phrase, not direct-address feedback, so it must NOT trailing-vocative fast-pass; (2) the bare-wake check.
_GREETING = frozenset(("hey", "hi", "hello", "heya", "hiya", "yo"))
# Tokens that, ALONE, make an utterance wake-only (nothing to act on): filler/politeness + greetings + the name.
_WAKE_ONLY_TOKENS = _FILLER_LEADS | _NAME_TOKENS | _GREETING


def _is_bare_wake(text: str) -> bool:
    """Decisively wake-only: the utterance is NOTHING but wake/greeting/filler tokens, and carries at
    least one greeting or the assistant's name — e.g. "hey aria", "hello", "okay aria". There is no
    command to act on, so per Rob's listen-first rule (VAC ask 2026-06-19) the brain stays silent rather
    than greeting back ("Hello, I'm here."). This is the ONE narrow case where the heuristic decides
    NOT-addressed; it requires EVERY token to be ignorable, so it can never eat a command (any substantive
    word — a verb, an object, ambient content like "we are" — defeats it and defers to the LLM instead).
    The merged-ambient case "we are hey aria" is NOT all-ignorable, so it falls through to the LLM."""
    words = [w.strip(".,!?;:\"'“”‘’") for w in text.strip().lower().split()]
    words = [w for w in words if w]
    if not words:
        return False
    if not all(w in _WAKE_ONLY_TOKENS for w in words):
        return False
    return any(w in _GREETING or w in _NAME_TOKENS for w in words)

# First-person openers that explicitly direct the request AT the assistant ("you"). These miss the
# lead-word fast-pass because the lead word is "I", so a clear command like "I want you to play a
# playlist" otherwise pays a ~12.7s LLM classify (a quarter of Rob's 2026-06-16 42s play). They name
# "you" as the actor → unambiguously addressed; fast-passing them is safe (the heuristic only ever
# short-circuits the addressed case). Kept to explicit "I … you" forms (no bare "let's", which would
# fast-pass thinking-aloud asides like "let's see"). The "want to dictate" self-label defers first.
_ADDRESSED_OPENERS = (
    "i want you", "i'd like you", "i would like you", "i need you", "i'd love you to",
    "i want to ask you", "i'd like for you", "i need for you",
)

# ROUND 2 (per VAC's Jun 6–7 mining: ~40/48 of the 15s runaway-turn cap-hits were Rob dictating long
# asides he EXPLICITLY marks as non-commands). These self-labels — the user declaring "I'm just
# dictating" or "you don't need to respond" — are strong NOT-addressed signals. When one is present we
# must NOT fast-pass on a leading command word ("MAKE a note for your overseers" otherwise fast-passes on
# "make") or an Aria mention — we defer to the LLM, which then classifies it as an aside. Deferring is
# SAFE: a false match only costs one LLM call (the model still answers a genuine command), so this set
# can be generous without ever eating a command. Substring match on the lowercased, filler-kept text.
_ASIDE_SELF_LABELS = (
    "dictating", "dictate this", "dictate something", "gonna dictate", "going to dictate",
    "want to dictate", "let me dictate", "i'll dictate", "i will dictate", "just dictation",
    "you don't need to respond", "you dont need to respond", "you don't have to respond",
    "you dont have to respond", "no need to respond", "don't respond to", "dont respond to",
    "you don't need to do anything", "you dont need to do anything", "you don't have to do anything",
    "nothing for you to do", "you don't need to act", "for the record", "on the record",
    "for your overseers", "claude overseers", "your overseers", "my overseers", "the overseers",
)


def _has_aside_self_label(t: str) -> bool:
    """True if the (lowercased) text carries an explicit non-command self-label — Rob telling the
    assistant he's just dictating / it needn't respond. Blocks the heuristic fast-pass; the LLM decides."""
    return any(p in t for p in _ASIDE_SELF_LABELS)


def _wake_only_likely(wake: object, threshold: float) -> bool:
    """Item C: True when the voice side's out-of-band signal says this utterance is very likely NOTHING
    but the wake word. `wake` is the dict the front-end attaches on /respond — only its (fused, voice-side,
    duration-dominant) `bare_wake_likelihood` drives the decision; the raw features (confidence,
    speech_dur_ms, …) ride along for receipt-tuning only. Defensive: a missing/non-numeric likelihood
    reads as 0.0 (no suppression), so a malformed signal can never trip the guard."""
    if not isinstance(wake, dict):
        return False
    try:
        likelihood = float(wake.get("bare_wake_likelihood"))
    except (TypeError, ValueError):
        return False
    return likelihood >= threshold


def _wake_fresh(wake: object) -> bool:
    """Lever A: True when the front-end marked this turn as a FRESH, clean-strip wake-LED utterance — the
    wake word led the turn and the voice side cleanly stripped the vocative, so what remains is a command
    addressed by construction. Mutually exclusive per turn with the garble-path `bare_wake_likelihood`
    (clean-strip => `fresh`; strip-failed => the likelihood object). The producer never sets `fresh` on an
    in-window follow-on / aside (aside-safety stays voice-side, where the timing is observable). Defensive:
    only a literal True fast-passes; absent/false/malformed => no fast-pass (exact current behavior)."""
    return isinstance(wake, dict) and wake.get("fresh") is True


def _wake_fresh_command(wake: object, user_text: str) -> bool:
    """True when a FRESH wake-led turn carries an actual command to act on. `fresh` means the wake word led
    the turn (vocative stripped voice-side) → addressed by construction, BUT the residual must be a real
    command: a bare "hey aria" (or an about-her "…is annoying" the producer scores 0) must still classify,
    not fast-pass. Prefers the producer's `residual_words` count (Rob's requested aside-safety hint); falls
    back to the residual text the brain already holds when the count is absent/non-integer. Either way an
    empty / zero-command residual returns False, so the fast-pass only fires on a genuine wake-led command."""
    if not _wake_fresh(wake):
        return False
    rw = wake.get("residual_words")
    if isinstance(rw, int):
        return rw >= 1
    return bool(user_text.strip())


# Item C — wake-context hint, prepended to _ADDRESSED_PROMPT ONLY when the acoustic signal says this turn
# is very likely a bare wake. Text alone can't tell a fluently mis-transcribed "Hey Aria" ("how are you?")
# from a genuine pleasantry — the duration-dominant `bare_wake_likelihood` already gated us here; the model
# just confirms the text is content-free. NEVER suppresses on its own: a genuine request still returns
# [ADDRESSED], so a wrong signal costs one cheap classify, never a dropped command.
_WAKE_CONTEXT_HINT = """\
CONTEXT: The voice front-end's wake detector fired for this turn and signals the audio was very likely
JUST the wake word, with little or no speech following — i.e. a bare wake ("Hey Aria") that speech-to-text
may have fluently mis-transcribed into a longer phrase. On these short wake clips the transcriber frequently
garbles or drops the words, so the text below may be a content-free greeting ("how are you", "good morning"),
a fragment, or even unrelated words — none of which is a real request.

The audio is the reliable evidence here, so DEFAULT to [ASIDE] (wake-only — she listens, she does not greet
back) UNLESS the text clearly carries an actionable request, a question to act on, or a command (e.g. "stop",
"play jazz", "what's the weather"). This OVERRIDES the usual "when unsure, answer" default: under a fired
wake, unsure means wake-only. Only a clear, genuine request earns [ADDRESSED].

"""

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

Also an ASIDE: the user explicitly DICTATING or narrating for the record — saying he's "just dictating",
that "you don't need to respond / do anything", or noting something "for your overseers" / "for the
record". These are declarations, NOT requests for Aria to act NOW, even if phrased like "make a note…".

Also an ASIDE: a bare greeting or simply being NAMED with no request — "hey Aria", "hello", or her name
surrounded by ambient words ("we are hey Aria"). Waking her is not a question; she listens, she does not
greet back. Only answer once an actual command or question follows.
Examples:
- "that was a false positive" → [ASIDE] (commentary about her)
- "the goal is it doesn't trip when I'm just talking" → [ASIDE]
- "he still hasn't bothered to respond, if you noticed" → [ASIDE] (narrating to someone else, "you" is about her)
- "isn't that incredible?" → [ASIDE] (rhetorical, not a request)
- "Mel, tell me something interesting" → [ASIDE] (addressed to Mel)
- "hey Aria" → [ASIDE] (wake-only — just being named, no request)
- "we are hey Aria" → [ASIDE] (ambient noise merged with the wake word, no request)
- "make a note for your Claude overseers that the volume is off" → [ASIDE] (dictating for the record, not a live request)
- "the music is… you don't need to do anything, I'm just dictating" → [ASIDE] (explicit non-command)
- "I'm gonna dictate something so it's on the record, you don't need to respond" → [ASIDE]
- "turn it up" / "what's the weather" / "it's too quiet in here" → [ADDRESSED]
- "I was talking to you — do you think you did okay?" → [ADDRESSED] (re-asking her directly after she didn't reply; a statement-form lead like "I was talking to you" is direct address, not narration)
- "that's a solid suggestion, thank you" → [ADDRESSED] (a direct acknowledgement or thanks TO Aria invites a brief reply)

When genuinely unsure, return [ADDRESSED].

Utterance: {text}"""


def _fast_verdict(text: str) -> bool | None:
    """Heuristic short-circuit. Returns True only for an *obviously addressed* utterance (a question,
    or one led by a command/question word). Returns None for everything else → defer to the model.
    NEVER returns False: the heuristic alone can never suppress a turn."""
    t = text.strip().lower()
    if not t:
        return None
    # An explicit dictation / "don't respond" self-label blocks every fast-pass below (command lead AND
    # Aria mention) and defers to the LLM — otherwise "make a note for your overseers" fast-passes on
    # "make" and an "...Aria..." aside fast-passes on the mention. (Safe: deferring never eats a command.)
    if _has_aside_self_label(t):
        return None
    # First-person openers that explicitly name "you" as the actor ("I want you to…", "let's…") are
    # addressed but lead on "I"/"let's", so they'd otherwise fall through to the costly LLM classify.
    if t.startswith(_ADDRESSED_OPENERS):
        return True
    # NB: a trailing "?" is deliberately NOT a fast-pass — rhetoricals ("isn't that wild?", "really?")
    # are asides, so anything question-shaped but not led by a question word goes to the classifier.
    # Genuine questions still fast-pass via their leading question word (what/how/is/are/can…) below.
    words = [w.strip(".,!?;:\"'") for w in t.split()]
    # Skip leading filler/politeness AND a leading vocative name to read the real lead word, so
    # "Aria, play X" / "hey Aria pause" fast-pass on the COMMAND that follows the name. A bare vocative
    # with nothing actionable after it ("aria", "hey aria") falls through to None and is handled by the
    # decisive _is_bare_wake / the LLM — naming her alone is no longer a fast-pass to "addressed" (it
    # produced the "Hello, I'm here." greeting-reply VAC flagged 2026-06-19). A mid-utterance MENTION
    # ("you have to say hey aria and then…") still defers: its lead word isn't filler/name.
    i = 0
    while i < len(words) and (words[i] in _FILLER_LEADS or words[i] in _NAME_TOKENS):
        i += 1
    lead = words[i] if i < len(words) else ""
    if lead in _COMMAND_LEADS:
        return True
    # Trailing/embedded VOCATIVE address: the assistant's name as the FINAL word ("what do you think, Arya?",
    # "your performance was solid, Aria") is a strong direct-address signal the lead-word check misses — these
    # were getting mis-asided by the LLM when no leading question/command word was present. Exclude narration
    # ABOUT her ("talking TO aria", "with aria") where a preposition precedes the name, AND a trailing WAKE
    # phrase ("…hey aria" — a greeting right before the name) which is wake-only, not feedback (the
    # ambient-merged "we are hey aria" leak, VAC 2026-06-19). Leans addressed otherwise (tolerate the rare
    # aside-answer; never drop a real address).
    if (len(words) >= 2 and words[-1] in _NAME_TOKENS
            and words[-2] not in _NARRATION_PRE and words[-2] not in _GREETING):
        return True
    return None


def _classify_client_model(ctx):
    """(client, model) for the is_addressed gate classify. Exclusive/offline local mode → the local model
    (the only one the Ollama endpoint knows). Claude provider → always the Claude floor (haiku). Else arya —
    EXCEPT when a Claude backend is available AND either Turbo is on (a declared bad-arya day) or the
    persistent `voice_fast_addressing_gate` flag is set, in which case the gate routes to the fast haiku
    classifier (closes the ~10s arya classify tax — the gate fires every turn and rode arya's cloud-latency
    variance, the dominant felt-latency tail on the Pi). Normal days with the flag off stay on arya (free).
    Mirrors the router's own classifier pick."""
    from gabagent.agent.loop import _active_client
    cfg = ctx.config
    # Exclusive/offline local mode: `_active_client(ctx)` returns the local Ollama client, which only knows
    # `local_model`. Pairing it with the Gab `simple_model` ("arya") 404s EVERY turn (NotFoundError) — the
    # gate then silently fails open, so the addressing filter is effectively off whenever she's on local
    # (incl. after an offline-failover). Classify on the model that's actually loaded. (Local-as-FLOOR is
    # unaffected: local_mode is False there, so the classify stays on the primary gab client + simple_model.)
    if getattr(ctx, "local_mode", False) and getattr(cfg, "local_model", ""):
        return _active_client(ctx), cfg.local_model
    if cfg.provider == "claude":
        return _active_client(ctx), cfg.claude.ladder[0].model
    if getattr(ctx, "turbo_commands", False) or getattr(cfg, "voice_fast_addressing_gate", False):
        try:
            from gabagent.api.factory import anthropic_configured
            if (anthropic_configured(cfg) and (cfg.router.cross_backend or cfg.provider == "claude")
                    and "claude" not in getattr(ctx, "degraded_backends", set())):
                return _active_client(ctx, "claude"), cfg.claude.ladder[0].model
        except Exception:
            pass
    return _active_client(ctx), cfg.router.simple_model


async def is_addressed(ctx: AgentContext, user_text: str, wake: dict | None = None) -> tuple[bool, str]:
    """Decide whether `user_text` is directed at the assistant. Returns (addressed, via) where `via`
    is one of 'wake_only' | 'wake_fresh' | 'fast' | 'llm:addressed' | 'llm:aside' | 'llm:wake_only' |
    'error'. Never raises; fails open (addressed) so a classifier hiccup can never drop a command. `wake` is
    the optional out-of-band acoustic wake signal (item C); absent => exact current behavior."""
    # Decisive wake-only: a bare greeting / being named with no command never gets a spoken reply
    # (listen-first). Narrow by construction — every token must be ignorable — so it can't eat a command.
    # Runs BEFORE the fresh fast-pass below, so a bare "hey aria" can never be fast-passed to addressed even
    # if the producer were to mark it fresh.
    if _is_bare_wake(user_text):
        return False, "wake_only"
    # Lever A: a FRESH clean-strip wake-LED turn carrying a real command is addressed by construction (the
    # wake word led it and the voice side stripped the vocative). Fast-pass it and skip the classify ENTIRELY
    # — no arya/haiku call at all, the cheapest possible gate, removing the per-turn classify cost on the
    # common wake-led case (the billing mitigation for the haiku gate). `_wake_fresh_command` requires a
    # non-zero residual (the producer's `residual_words`, else the residual text), so a bare "hey aria" or an
    # about-her "…is annoying" still classifies — dodging wake-led false-address. Gated behind the same
    # wake-signal consumer switch; safe-inert when the producer omits `fresh` (absent => no fast-pass).
    if getattr(ctx.config, "voice_wake_confidence_filter", True) and _wake_fresh_command(wake, user_text):
        return True, "wake_fresh"
    # Item C: when the voice side's acoustic signal says this turn is very likely a bare wake (a "Hey Aria"
    # the STT fluently rewrote into a question), DON'T trust the text fast-pass — "how are you" would
    # otherwise fast-pass on the question word "how". Demote to the LLM classify with a wake-context hint.
    # This NEVER suppresses on its own (a real command still classifies ADDRESSED below); the worst a wrong
    # signal does is cost one cheap classify on a genuine command the LLM still answers.
    wake_suspect = (
        getattr(ctx.config, "voice_wake_confidence_filter", True)
        and _wake_only_likely(wake, getattr(ctx.config, "voice_bare_wake_threshold", 0.8))
    )
    if wake_suspect:
        from gabagent.voice.debuglog import dlog
        dlog(ctx, "wake_signal", suspect=True, wake=wake)
    if not wake_suspect:
        fast = _fast_verdict(user_text)
        if fast:
            return True, "fast"
    try:
        # Backend-appropriate cheap model + its client. On the Claude backend the simple model is the
        # ladder's bottom rung (haiku) — passing the Gab `simple_model` ("arya") to Anthropic 400s, which
        # is caught below and silently fails OPEN, bypassing the whole filter (the LIVE 2026-06-08
        # Claude-backend bug: 28 asides leaked because every deferred classify errored). Mirrors turn.py's
        # `simple` pick — and in Turbo (bad-arya day) routes this gate onto the fast haiku classifier too.
        client, model = _classify_client_model(ctx)
        # Receipt for the gate's classifier model — lets a joint drive confirm the gate ran on the fast
        # haiku path (vs arya) and join its latency against the `addressed` event. No-op unless voice_debug.
        from gabagent.voice.debuglog import dlog
        dlog(ctx, "gate_model", model=model)
        prompt = _ADDRESSED_PROMPT.format(text=user_text.strip())
        if wake_suspect:
            prompt = _WAKE_CONTEXT_HINT + prompt
        messages = [ChatMessage(role="user", content=prompt)]
        tag = await client.complete_simple(messages, model=model)
        if "[ASIDE]" in tag.upper() and "[ADDRESSED]" not in tag.upper():
            # Mark a wake-signal-assisted suppression distinctly from an ordinary aside, for receipt tuning.
            return False, "llm:wake_only" if wake_suspect else "llm:aside"
        return True, "llm:addressed"
    except Exception as exc:
        # Fail OPEN (never eat a command) but surface WHY in `via` — a bare "error" hid the model-mismatch
        # bug above through a whole live session. The class name is enough to spot a systemic failure.
        return True, f"error:{type(exc).__name__}"
