import types
import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.voice.addressed import _fast_verdict, _wake_only_likely, is_addressed


# ---- _fast_verdict: heuristic short-circuit (True = obviously addressed, None = defer to model) ----

@pytest.mark.parametrize("text", [
    "play Metallica",
    "turn it up",
    "pause the movie",
    "what's the weather",
    "how do I get there?",          # question mark
    "hey, play Bad Omens",          # leading filler stripped
    "okay turn it down",
    "please pause",
    "Aria, what time is it",        # names the assistant
])
def test_fast_verdict_obvious_commands_are_addressed(text):
    assert _fast_verdict(text) is True


@pytest.mark.parametrize("text", [
    "damn it",
    "the word dammit seems to cause the",
    "so the whole goal is that the voice doesn't trip when just talking",
    "that was a false positive",
    "ugh, never mind",
    "isn't that incredible?",        # rhetorical: trailing "?" is NOT a fast-pass anymore
    "really?",                       # rhetorical question, no question-word lead → defer
    "he still hasn't bothered to respond if you noticed",  # 2nd-person ABOUT the bot → defer
    "",                              # empty → defer (not a hard True)
])
def test_fast_verdict_ambiguous_defers_to_model(text):
    # Heuristic NEVER hard-suppresses — ambiguity is None (→ LLM), never False. A trailing "?" alone
    # no longer fast-passes (rhetoricals); genuine questions still fast-pass via their question word.
    assert _fast_verdict(text) is None


# ---- ROUND 2: explicit dictation / "don't respond" self-labels block the fast-pass (→ defer to LLM) ----

@pytest.mark.parametrize("text", [
    "make a note for your Claude overseers that the volume is off",  # leads with "make" — must NOT fast-pass
    "Aria, you don't need to respond to me",                         # mentions Aria — must NOT fast-pass
    "the music is fine, you don't need to do anything, I'm just dictating",
    "I'm gonna dictate something so it's on the record",
    "set this aside, for the record I'm only dictating",
    "no need to respond, just making a note",
])
def test_fast_verdict_self_labeled_asides_defer_not_fastpass(text):
    # These carry an explicit non-command self-label; the heuristic must defer (None), NOT fast-pass True
    # on a leading command word or an Aria mention. (Deferring is safe — the LLM still answers real commands.)
    assert _fast_verdict(text) is None


async def test_self_labeled_command_lead_reaches_llm_and_suppresses():
    # "make a note for your overseers" used to fast-pass on "make"; now it reaches the LLM, which asides it.
    addressed, via = await is_addressed(_ctx("[ASIDE]"), "make a note for your Claude overseers")
    assert addressed is False and via == "llm:aside"


def test_fast_verdict_real_command_without_self_label_still_fastpasses():
    # Regression: an ordinary "make"/"play" command (no self-label) must still fast-pass with zero latency.
    assert _fast_verdict("make a playlist of jazz") is True
    assert _fast_verdict("play that album") is True


@pytest.mark.parametrize("text", [
    "I want you to play a playlist, something with a retro feel",  # the live 2026-06-16 42s case
    "I'd like you to turn the lights down",
    "I need you to pause that",
    "I would like you to read me the news",
    "I want to ask you something",
])
def test_fast_verdict_first_person_directed_openers_fastpass(text):
    # "I want you to…" explicitly names "you" as the actor → addressed, but leads on "I" so it used to
    # pay a ~12.7s LLM classify. Now it fast-passes with zero latency.
    assert _fast_verdict(text) is True


@pytest.mark.parametrize("text", [
    "I want to dictate something for the record",  # self-label still defers (guard runs first)
    "let's see what happens here",                 # bare "let's" deliberately NOT an opener → defer
    "I think that went well",                      # first-person but not directed at "you" → defer
])
def test_fast_verdict_first_person_non_directed_still_defers(text):
    assert _fast_verdict(text) is None


@pytest.mark.parametrize("text", [
    "aria what time is it",          # leading name
    "hey aria play something",       # filler + leading name
    "okay aria, pause",
])
def test_fast_verdict_vocative_aria_fastpasses(text):
    assert _fast_verdict(text) is True


@pytest.mark.parametrize("text", [
    "you have to say hey aria and then give her a second",  # 3rd-person MENTION (live leak, round-1 gap a)
    "the voice of aria sounds really nice",                 # mention, not address
    "i think aria misheard that",                           # commentary about her
])
def test_fast_verdict_aria_mention_defers_not_fastpass(text):
    # A bare "aria" mid-utterance is a mention, not vocative address — must defer to the LLM (None),
    # not fast-pass. (The old aria-anywhere rule leaked these as answered.)
    assert _fast_verdict(text) is None


@pytest.mark.parametrize("text", [
    "your performance was solid, arya",     # trailing vocative, NO leading question/command word
    "what do you think of that, arya",
    "did you hear me, aria",
])
def test_fast_verdict_trailing_vocative_fastpasses(text):
    # A trailing/embedded vocative ("…, Arya") is direct address the lead-word check misses — these were
    # getting mis-asided by the LLM. (Rob, live 2026-06-15.)
    assert _fast_verdict(text) is True


@pytest.mark.parametrize("text", [
    "i was just talking to aria",           # narration ABOUT her — preposition before the trailing name
    "i'm here with aria",
    "i was chatting with aria",
])
def test_fast_verdict_name_in_narration_still_defers(text):
    # The name as the last word but preceded by a narration preposition ("to/with/tell aria") is talking
    # ABOUT her, not TO her → defer, don't fast-pass.
    assert _fast_verdict(text) is None


@pytest.mark.parametrize("text", [
    "we are hey aria",      # ambient STT noise + a TRAILING wake phrase — the 2026-06-19 "Hello, I'm here." leak
    "hey aria",             # bare wake — naming her alone no longer fast-passes as addressed
    "aria",                 # bare vocative
    "hello aria",           # greeting + name, no command
])
def test_fast_verdict_bare_wake_defers_not_fastpass(text):
    # Naming/greeting Aria with no command must NOT fast-pass to "addressed" — a trailing "<greeting> aria"
    # is a wake phrase, not feedback, and a lead name is skipped so only a real command after it fast-passes.
    # (Suppression itself is decided by is_addressed/_is_bare_wake; here we assert the heuristic defers.)
    assert _fast_verdict(text) is None


def _ctx(tag, *, raises=False, seen=None, provider="gab"):
    class _Client:
        async def complete_simple(self, messages, model=None):
            if seen is not None:
                seen.append(model)
            if raises:
                raise RuntimeError("classifier down")
            return tag
    cfg = GabAgentConfig(api_key="t")
    cfg.provider = provider
    return types.SimpleNamespace(
        config=cfg,
        client=_Client(),
        local_mode=False,
        local_client=None,
    )


async def test_classifier_uses_backend_appropriate_model():
    # The LIVE 2026-06-08 bug: the filter hardcoded the Gab simple_model ("arya") and shipped it to the
    # Claude backend, which 400s → fails open → whole filter bypassed. On Claude it must use the ladder's
    # bottom rung (haiku); on Gab it stays arya.
    seen_claude: list = []
    await is_addressed(_ctx("[ASIDE]", seen=seen_claude, provider="claude"), "some aside here")
    assert seen_claude == [GabAgentConfig(api_key="t").claude.ladder[0].model]
    assert seen_claude[0].startswith("claude-")

    seen_gab: list = []
    await is_addressed(_ctx("[ASIDE]", seen=seen_gab, provider="gab"), "some aside here")
    assert seen_gab == [GabAgentConfig(api_key="t").router.simple_model]


async def test_classifier_uses_local_model_in_local_mode():
    # LIVE 2026-06-22: in exclusive/offline local mode the gate handed the Gab simple_model ("arya") to the
    # local Ollama client → 404 NotFoundError EVERY turn → the filter silently failed open (addressing gate
    # off whenever she's on local, incl. after an offline-failover). Must classify on the loaded local model.
    seen: list = []

    class _Local:
        async def complete_simple(self, messages, model=None):
            seen.append(model)
            return "[ASIDE]"

    cfg = GabAgentConfig(api_key="t")
    cfg.provider = "gab"
    cfg.local_model = "devstral:24b"
    ctx = types.SimpleNamespace(config=cfg, client=_Local(), local_mode=True, local_client=_Local())
    addressed, via = await is_addressed(ctx, "some aside here")
    assert seen == ["devstral:24b"]                 # classified on the local model, NOT "arya"
    assert addressed is False and via == "llm:aside"


async def test_is_addressed_fast_path_skips_llm():
    # An obvious command never reaches the classifier (would raise if it did).
    addressed, via = await is_addressed(_ctx("[ASIDE]", raises=True), "play Metallica")
    assert addressed is True and via == "fast"


async def test_is_addressed_llm_aside_suppresses():
    addressed, via = await is_addressed(_ctx("[ASIDE]"), "damn it")
    assert addressed is False and via == "llm:aside"


async def test_is_addressed_llm_addressed_passes():
    addressed, via = await is_addressed(_ctx("[ADDRESSED]"), "it's too quiet in here")
    assert addressed is True and via == "llm:addressed"


async def test_is_addressed_unknown_tag_fails_open():
    # An unparseable classifier reply must NOT eat a command — default addressed.
    addressed, via = await is_addressed(_ctx("[SIMPLE]"), "mumble something")
    assert addressed is True and via == "llm:addressed"


async def test_is_addressed_classifier_error_fails_open():
    # Fails open (never eats a command) and surfaces the exception class in `via` so a systemic
    # classifier failure (e.g. a Gab model name sent to the Claude backend) isn't silently hidden.
    addressed, via = await is_addressed(_ctx("[ASIDE]", raises=True), "some aside here")
    assert addressed is True and via.startswith("error:")


# ---- listen-first: a wake/greeting with no command never gets a spoken reply (VAC ask 2026-06-19) ----

@pytest.mark.parametrize("text", ["hey aria", "hello", "okay aria", "aria", "hey, aria."])
async def test_is_addressed_bare_wake_is_silent_without_llm(text):
    # A wake/greeting with no command is decisively wake-only — stay silent, and DON'T pay an LLM classify
    # (raises=True would blow up if the classifier were consulted).
    addressed, via = await is_addressed(_ctx("[ADDRESSED]", raises=True), text)
    assert addressed is False and via == "wake_only"


async def test_is_addressed_ambient_merged_wake_reaches_llm_and_asides():
    # STT merged ambient audio with the wake ("we are hey aria") so it isn't all-ignorable → not decisive;
    # it reaches the LLM, which asides it. This is the exact 2026-06-19 leak ("We are Hey, Aria." → reply).
    addressed, via = await is_addressed(_ctx("[ASIDE]"), "we are hey aria")
    assert addressed is False and via == "llm:aside"


async def test_is_addressed_wake_then_command_still_answers():
    # A real command after the wake must still fast-pass (the guard never eats a command).
    addressed, via = await is_addressed(_ctx("[ASIDE]", raises=True), "hey aria play some music")
    assert addressed is True and via == "fast"


async def test_is_addressed_bare_affirmation_is_not_wake_suppressed():
    # "yes"/"no" carry no greeting or name → NOT decisively wake-only, so a spoken confirm answer is never
    # silently eaten by this guard; it defers to the LLM instead.
    _, via = await is_addressed(_ctx("[ADDRESSED]"), "yes")
    assert via != "wake_only"


# ---- item C: out-of-band acoustic wake signal (STT wake-expansion: "Hey Aria" → "how are you?") ----

_HI_WAKE = {"bare_wake_likelihood": 0.9, "confidence": 0.97, "post_wake_voiced_ms": 120, "speech_dur_ms": 700}
_LO_WAKE = {"bare_wake_likelihood": 0.3, "confidence": 0.97, "post_wake_voiced_ms": 900, "speech_dur_ms": 1700}


@pytest.mark.parametrize("wake,expected", [
    (_HI_WAKE, True),
    ({"bare_wake_likelihood": 0.8}, True),     # boundary: >= threshold
    (_LO_WAKE, False),
    ({"bare_wake_likelihood": None}, False),   # malformed → never suppresses
    ({"confidence": 0.99}, False),             # likelihood missing → 0.0
    ({"bare_wake_likelihood": "x"}, False),    # non-numeric → never suppresses
    (None, False),
    ("not-a-dict", False),
])
def test_wake_only_likely(wake, expected):
    assert _wake_only_likely(wake, 0.8) is expected


async def test_high_wake_signal_demotes_fastpass_and_suppresses_misheard_bare_wake():
    # "how are you" would normally fast-pass on the question word "how". A high bare-wake signal demotes it
    # to the LLM classify (with the wake-context hint), which asides it → wake-only/silent. THE item-C fix.
    addressed, via = await is_addressed(_ctx("[ASIDE]"), "how are you", wake=_HI_WAKE)
    assert addressed is False and via == "llm:wake_only"


async def test_high_wake_signal_suppresses_garbled_fragment():
    # The common case (VAC's STT eval): a high wake signal but the STT garbled the name into a fragment /
    # unrelated words (not a tidy pleasantry). Under a fired wake the hint defaults non-actionable text to
    # wake-only, so the classifier asides it → silence (not the base "unsure → answer" default).
    addressed, via = await is_addressed(_ctx("[ASIDE]"), "the where of it", wake=_HI_WAKE)
    assert addressed is False and via == "llm:wake_only"


async def test_high_wake_signal_still_answers_a_real_command():
    # Even with a (wrongly) high wake signal, a genuine command survives: the signal only demotes the
    # fast-pass to the LLM, which returns ADDRESSED. The "never eat a command" invariant holds.
    addressed, via = await is_addressed(_ctx("[ADDRESSED]"), "play some jazz", wake=_HI_WAKE)
    assert addressed is True and via == "llm:addressed"


async def test_low_wake_signal_leaves_fastpass_intact():
    # Below threshold → behaves exactly as today: "how are you" fast-passes (raises=True proves no LLM call).
    addressed, via = await is_addressed(_ctx("[ADDRESSED]", raises=True), "how are you", wake=_LO_WAKE)
    assert addressed is True and via == "fast"


async def test_wake_signal_killswitch_off_leaves_fastpass_intact():
    # voice_wake_confidence_filter=False ignores the signal entirely → fast-pass survives (no LLM call).
    ctx = _ctx("[ADDRESSED]", raises=True)
    ctx.config.voice_wake_confidence_filter = False
    addressed, via = await is_addressed(ctx, "how are you", wake=_HI_WAKE)
    assert addressed is True and via == "fast"


async def test_no_wake_signal_is_exact_current_behavior():
    # Absent `wake` => arrival-keyed no-op: "how are you" fast-passes as today (safe ahead of the producer).
    addressed, via = await is_addressed(_ctx("[ADDRESSED]", raises=True), "how are you")
    assert addressed is True and via == "fast"


async def test_high_wake_signal_does_not_override_decisive_bare_wake():
    # A clean "hey aria" is still caught by the text-only _is_bare_wake first (no LLM), signal or not.
    addressed, via = await is_addressed(_ctx("[ASIDE]", raises=True), "hey aria", wake=_HI_WAKE)
    assert addressed is False and via == "wake_only"


# ---- Lever A: a FRESH clean-strip wake-led turn fast-passes addressed, skipping the classify entirely ----

# Locked marker shape (VAC producer): fresh + residual_words (command-word count) + confidence.
_FRESH_WAKE = {"fresh": True, "residual_words": 2, "confidence": 0.93}


async def test_wake_fresh_fastpasses_without_any_classify():
    # A fresh wake-led command (residual_words>=1) is addressed by construction → no LLM call (seen empty).
    seen: list = []
    addressed, via = await is_addressed(
        _ctx("[ASIDE]", raises=True, seen=seen), "play some jazz", wake=_FRESH_WAKE)
    assert addressed is True and via == "wake_fresh"
    assert seen == []  # the classify never ran — the cheapest gate


async def test_wake_fresh_zero_residual_words_classifies_dodging_about_her():
    # residual_words==0 (a bare/about-her wake-led turn the producer scored 0) must NOT fast-pass on the
    # fresh path — it falls through to normal handling. Text with no command/question lead defers to the
    # classifier, proving the fresh fast-pass was skipped (so "Hey Aria is annoying"-type false-address
    # is dodged by the count, not the fresh marker).
    seen: list = []
    addressed, via = await is_addressed(
        _ctx("[ASIDE]", seen=seen), "the meeting ran long", wake={"fresh": True, "residual_words": 0})
    assert via != "wake_fresh"
    assert seen != [] and via == "llm:aside"  # fresh path skipped → reached the classifier


async def test_wake_fresh_does_not_override_decisive_bare_wake():
    # Safety belt: even if the producer mis-marked a bare "hey aria" fresh, _is_bare_wake wins → wake_only.
    addressed, via = await is_addressed(
        _ctx("[ASIDE]", raises=True), "hey aria", wake=_FRESH_WAKE)
    assert addressed is False and via == "wake_only"


async def test_wake_fresh_killswitch_off_falls_through_to_classify():
    # voice_wake_confidence_filter=False disables the fresh fast-pass → the turn reaches the classifier.
    ctx = _ctx("[ADDRESSED]")
    ctx.config.voice_wake_confidence_filter = False
    addressed, via = await is_addressed(ctx, "the where of it", wake=_FRESH_WAKE)
    assert addressed is True and via == "llm:addressed"


async def test_wake_without_fresh_marker_is_unaffected():
    # `wake` present but no `fresh` (garble-path likelihood object) => no fast-pass; normal path.
    addressed, via = await is_addressed(
        _ctx("[ADDRESSED]", raises=True), "play some jazz", wake={"bare_wake_likelihood": 0.1})
    assert addressed is True and via == "fast"


async def test_wake_fresh_no_count_falls_back_to_residual_text():
    # When the producer omits residual_words, the consumer falls back to the residual text it holds: a bare
    # "hey aria" stripped to an EMPTY residual must NOT fast-pass (_is_bare_wake returns False on empty).
    seen: list = []
    addressed, via = await is_addressed(_ctx("[ASIDE]", seen=seen), "   ", wake={"fresh": True})
    assert via != "wake_fresh"  # blocked the empty-residual fast-pass
    assert addressed is False and via == "llm:aside"  # fell through to the classifier, which asided it


# ---- turn-level: a not-addressed utterance closes silently, never reaching the LLM ----

async def test_not_addressed_turn_closes_silently(tmp_path):
    import gabagent.agent.context as _ctxmod
    from gabagent.agent.context import AgentContext
    from gabagent.voice.session import VoiceSession
    from gabagent.voice.turn import start_turn, drain
    from gabagent.permissions.voice_approve import voice_approve

    streamed = {"called": False}

    class _Session:
        def __init__(self): self._m = []
        def messages(self): return list(self._m)
        def append_message(self, m): self._m.append(m)

    class _Client:
        model = "arya"
        async def complete_simple(self, messages, model=None):
            return "[ASIDE]"                         # ambiguous → classified as an aside
        async def stream_complete(self, *a, **k):
            streamed["called"] = True               # must NOT run for an aside
            yield "should not happen"

    cfg = GabAgentConfig(api_key="t")
    cfg.router.enabled = False
    sess = _Session()
    ctx = AgentContext(
        config=cfg, client=_Client(),
        rate_limiter=types.SimpleNamespace(record=lambda *a, **k: None),
        session=sess, session_id="s", cwd=tmp_path, system_prompt="", headless=True,
    )
    ctx.voice_mode = True
    ctx.approval_hook = voice_approve
    ctx.voice_session = VoiceSession("s", ctx)

    vs = ctx.voice_session
    start_turn(ctx, vs, "damn it")
    evs = [ev async for ev in drain(vs)]

    assert streamed["called"] is False              # the LLM was never reached
    assert sess.messages() == []                    # nothing appended to history
    assert not any(e.type == "token" for e in evs)  # no spoken reply
    assert evs and evs[-1].type == "done"           # turn closed cleanly


# ---- B2: data-mined fast-path widening (real deferred-addressed turns, 2026-06-22) ----------------

@pytest.mark.parametrize("text", [
    "push play",
    "push play on the movie",
    "watch the movie",
    "shuffle the playlist",
    "shuffle my retro favorites playlist",
    "jump to the 45 minute mark",
    "rewind the movie",
    "let's play a movie, you pick",
    "let's watch a movie on jellyfin",
    "let's jump to the 50 minute mark",
    "let's pause the movie",
    "let's close that window",
    "let's turn the music down to 40%",
])
def test_fast_verdict_b2_media_verbs_and_lets_fastpass(text):
    assert _fast_verdict(text) is True


@pytest.mark.parametrize("text", [
    "let's see what happens",          # thinking aloud — "see" isn't a command verb → must still defer
    "let's think about it",
])
def test_fast_verdict_b2_lets_thinking_aloud_still_defers(text):
    # The safe boundary: stripping "let's" only fast-passes when a COMMAND verb follows, never bare musing.
    assert _fast_verdict(text) is None


# ---- B1: Turbo routes the gate classify to the fast Claude rung (haiku), else stays on arya ----------

def _ctx_turbo(tag, *, seen, turbo, cross_backend=True, anthropic=True, fast_gate=False, arya_gate=False):
    class _GabClient:
        backend = "gab"
        async def complete_simple(self, messages, model=None):
            seen.append(("gab", model)); return tag
    class _ClaudeClient:
        backend = "claude"
        async def complete_simple(self, messages, model=None):
            seen.append(("claude", model)); return tag
    cfg = GabAgentConfig(api_key="t")
    cfg.provider = "gab"
    cfg.claude.api_key = "ankey" if anthropic else ""
    cfg.router.cross_backend = cross_backend
    cfg.voice_fast_addressing_gate = fast_gate
    cfg.voice_arya_addressing_gate = arya_gate
    return types.SimpleNamespace(
        config=cfg, client=_GabClient(), clients={"claude": _ClaudeClient()},
        local_mode=False, local_client=None, degraded_backends=set(),
        turbo_commands=turbo,
    )


async def test_b1_turbo_routes_gate_classify_to_haiku():
    seen: list = []
    await is_addressed(_ctx_turbo("[ASIDE]", seen=seen, turbo=True), "some ambiguous thing")
    assert len(seen) == 1
    backend, model = seen[0]
    assert backend == "claude" and model == GabAgentConfig(api_key="t").claude.ladder[0].model


async def test_no_turbo_defaults_to_haiku_when_claude_available():
    # option-b (2026-06-26): with a Claude backend available cross-backend and NEITHER turbo nor the explicit
    # fast-gate flag set, the gate now PREFERS haiku by default (was arya — the invisible-config trap on a
    # fresh satellite). The arya escape hatch is off by default.
    seen: list = []
    await is_addressed(_ctx_turbo("[ASIDE]", seen=seen, turbo=False), "some ambiguous thing")
    assert len(seen) == 1
    backend, model = seen[0]
    assert backend == "claude" and model == GabAgentConfig(api_key="t").claude.ladder[0].model


async def test_arya_escape_hatch_forces_gate_back_to_arya():
    # voice_arya_addressing_gate=True restores the historical free arya gate even when Claude is available.
    seen: list = []
    await is_addressed(_ctx_turbo("[ASIDE]", seen=seen, turbo=False, arya_gate=True), "some ambiguous thing")
    assert seen == [("gab", GabAgentConfig(api_key="t").router.simple_model)]


async def test_turbo_overrides_arya_escape_hatch():
    # Explicit opt-in to haiku (Turbo) wins over the arya escape hatch.
    seen: list = []
    await is_addressed(_ctx_turbo("[ASIDE]", seen=seen, turbo=True, arya_gate=True), "some ambiguous thing")
    assert len(seen) == 1 and seen[0][0] == "claude"


async def test_default_falls_to_arya_when_no_claude():
    # No Claude backend available → precondition fails → arya (safe no-op), even though haiku is preferred.
    seen: list = []
    await is_addressed(_ctx_turbo("[ASIDE]", seen=seen, turbo=False, anthropic=False), "some ambiguous thing")
    assert seen == [("gab", GabAgentConfig(api_key="t").router.simple_model)]


async def test_b1_turbo_falls_back_to_arya_when_no_claude():
    seen: list = []
    await is_addressed(_ctx_turbo("[ASIDE]", seen=seen, turbo=True, anthropic=False), "some ambiguous thing")
    assert seen == [("gab", GabAgentConfig(api_key="t").router.simple_model)]


# ---- Lever B: voice_fast_addressing_gate routes the gate to haiku WITHOUT Turbo (every turn) ----------

async def test_fast_gate_routes_to_haiku_without_turbo():
    seen: list = []
    await is_addressed(_ctx_turbo("[ASIDE]", seen=seen, turbo=False, fast_gate=True), "some ambiguous thing")
    assert len(seen) == 1
    backend, model = seen[0]
    assert backend == "claude" and model == GabAgentConfig(api_key="t").claude.ladder[0].model


async def test_fast_gate_off_now_defaults_to_haiku_unless_arya_hatch():
    # Was "fast_gate off → arya". As of option-b (2026-06-26) fast_gate off DEFAULTS to haiku when Claude is
    # available; arya now requires the explicit escape hatch. Both branches asserted here.
    seen: list = []
    await is_addressed(_ctx_turbo("[ASIDE]", seen=seen, turbo=False, fast_gate=False), "some ambiguous thing")
    assert seen == [("claude", GabAgentConfig(api_key="t").claude.ladder[0].model)]
    seen2: list = []
    await is_addressed(_ctx_turbo("[ASIDE]", seen=seen2, turbo=False, fast_gate=False, arya_gate=True),
                       "some ambiguous thing")
    assert seen2 == [("gab", GabAgentConfig(api_key="t").router.simple_model)]


async def test_fast_gate_falls_back_to_arya_when_no_claude():
    seen: list = []
    await is_addressed(
        _ctx_turbo("[ASIDE]", seen=seen, turbo=False, fast_gate=True, anthropic=False), "some ambiguous thing")
    assert seen == [("gab", GabAgentConfig(api_key="t").router.simple_model)]
