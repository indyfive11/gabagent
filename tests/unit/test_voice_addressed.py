import types
import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.voice.addressed import _fast_verdict, is_addressed


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
