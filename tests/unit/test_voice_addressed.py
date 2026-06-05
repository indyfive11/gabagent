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
    "",                              # empty → defer (not a hard True)
])
def test_fast_verdict_ambiguous_defers_to_model(text):
    # Heuristic NEVER hard-suppresses — ambiguity is None (→ LLM), never False.
    assert _fast_verdict(text) is None


def _ctx(tag, *, raises=False):
    class _Client:
        async def complete_simple(self, messages, model=None):
            if raises:
                raise RuntimeError("classifier down")
            return tag
    return types.SimpleNamespace(
        config=GabAgentConfig(api_key="t"),
        client=_Client(),
        local_mode=False,
        local_client=None,
    )


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
    addressed, via = await is_addressed(_ctx("[ASIDE]", raises=True), "some aside here")
    assert addressed is True and via == "error"


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
