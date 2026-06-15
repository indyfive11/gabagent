import json
import types
from pathlib import Path
import pytest

from gabagent.api.models import ChatMessage, ToolCallSpec
from gabagent.config.models import GabAgentConfig
from gabagent.agent.context import AgentContext
from gabagent.voice.session import VoiceSession
from gabagent.voice.turn import start_turn, drain
from gabagent.permissions.voice_approve import voice_approve
import gabagent.tools.file_tools  # noqa: F401  (registers read_file/write_file/edit)


class FakeSession:
    def __init__(self):
        self._msgs = []

    def messages(self):
        return list(self._msgs)

    def append_message(self, m):
        self._msgs.append(m)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.model = "arya"

    async def stream_complete(self, messages, tools=None, model=None, retry_model=None, **kw):
        chunks = self.responses.pop(0)
        for c in chunks:
            yield c

    async def complete_simple(self, messages, model=None):
        return "[SIMPLE]"


def _spec(name, **args):
    return ToolCallSpec(id=name + "-1", name=name, arguments=json.dumps(args))


def make_ctx(tmp_path, responses, **cfg_kw):
    cfg = GabAgentConfig(api_key="test", **cfg_kw)
    cfg.router.enabled = False
    ctx = AgentContext(
        config=cfg,
        client=FakeClient(responses),
        rate_limiter=types.SimpleNamespace(record=lambda *a, **k: None),
        session=FakeSession(),
        session_id="s",
        cwd=tmp_path,
        system_prompt="",
        headless=True,
    )
    ctx.voice_mode = True
    ctx.approval_hook = voice_approve
    ctx.voice_session = VoiceSession("s", ctx)
    return ctx


async def run_turn(ctx, text, answer=None):
    """Drive a full turn across the two-phase confirm protocol: drain to the first
    confirm/done, then for each confirm resolve it and drain the continuation."""
    vs = ctx.voice_session
    evs = []
    start_turn(ctx, vs, text)
    async for ev in drain(vs):
        evs.append(ev)
    while evs and evs[-1].type == "confirm" and answer is not None:
        vs.resolve(evs[-1].id, answer)
        async for ev in drain(vs):
            evs.append(ev)
    return evs


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


async def test_aside_emits_addressed_false_then_done(home, monkeypatch):
    """A suppressed aside emits a standalone `addressed:false` event (the A1 movie-duck-release signal
    the voice client acts on) immediately before `done`, with no reply token and no history append."""
    import gabagent.voice.addressed as addr
    async def _aside(ctx, text):
        return False, "llm:aside"
    monkeypatch.setattr(addr, "is_addressed", _aside)
    proj = home / "proj"; proj.mkdir()
    ctx = make_ctx(proj, [["should never speak"]])
    evs = await run_turn(ctx, "the voice of Aria is nice")
    types_ = [e.type for e in evs]
    assert types_ == ["addressed", "done"]                       # exactly the signal + close, nothing else
    a = next(e for e in evs if e.type == "addressed")
    assert a.to_dict() == {"type": "addressed", "addressed": False}   # VAC wire shape, false present
    assert not any(e.type == "token" for e in evs)               # no spoken reply for an aside
    assert ctx.session.messages() == []                          # not appended to history


async def test_tier1_scratch_write_streams_and_writes(home):
    proj = home / "proj"
    proj.mkdir()
    dest = home / "voice-scratch" / "hello.txt"
    ctx = make_ctx(proj, [
        ["Okay. ", "On it.", [_spec("write_file", path=str(dest), content="hi there")]],
        ["Saved it."],
    ])
    evs = await run_turn(ctx, "draft a hello note")
    assert not any(e.type == "confirm" for e in evs)          # Tier 1, no gate
    assert any(e.type == "token" for e in evs)
    assert any(e.type == "done" for e in evs)
    assert dest.read_text() == "hi there"


async def test_tier2_edit_requires_spoken_confirm(home):
    proj = home / "proj"
    proj.mkdir()
    target = proj / "readme.txt"
    target.write_text("line one\n")
    ctx = make_ctx(proj, [
        [[_spec("edit", path=str(target), old_string="line one", new_string="line two")]],
        ["Done."],
    ])
    evs = await run_turn(ctx, "edit the readme", answer=True)
    confirms = [e for e in evs if e.type == "confirm"]
    assert confirms and confirms[0].method == "spoken_yesno" and confirms[0].tier == 2
    assert target.read_text() == "line two\n"


async def test_tier3_outside_write_denied_not_run(home):
    proj = home / "proj"
    proj.mkdir()
    dest = home / "outside" / "x.txt"
    ctx = make_ctx(proj, [
        [[_spec("write_file", path=str(dest), content="should not exist")]],
        ["Okay."],
    ])
    evs = await run_turn(ctx, "write outside", answer=False)
    confirms = [e for e in evs if e.type == "confirm"]
    assert confirms and confirms[0].method == "keyboard" and confirms[0].tier == 3
    assert not dest.exists()


async def test_vpn_warning_only_when_networked(home):
    proj = home / "proj"
    proj.mkdir()
    # bash is gated at Tier 3; deny so nothing runs.
    ctx = make_ctx(proj, [[[_spec("bash", command="vpn-full")]], ["ok"]])
    evs = await run_turn(ctx, "toggle vpn", answer=False)
    confirms = [e for e in evs if e.type == "confirm"]
    assert confirms and "network" in confirms[0].reason.lower()

    ctx2 = make_ctx(proj, [[[_spec("bash", command="vpn-full")]], ["ok"]])
    ctx2.local_mode = True
    evs2 = await run_turn(ctx2, "toggle vpn", answer=False)
    c2 = [e for e in evs2 if e.type == "confirm"]
    assert c2 and "network" not in c2[0].reason.lower()


async def test_code_only_response_not_spoken(home):
    proj = home / "proj"
    proj.mkdir()
    ctx = make_ctx(proj, [["```python\nprint('secret')\n```"]])
    evs = await run_turn(ctx, "show me the code")
    assert not any(e.type == "token" for e in evs)            # code never spoken
    assert any(e.type == "status" for e in evs)               # but a "drafted code" status


async def test_escalation_emits_status(home):
    proj = home / "proj"
    proj.mkdir()
    ctx = make_ctx(proj, [["All set."]])
    ctx.active_model = ctx.config.router.complex_model        # pretend router escalated
    evs = await run_turn(ctx, "do something complex")
    assert any(e.type == "status" for e in evs)


async def test_cancel_ends_turn(home):
    proj = home / "proj"
    proj.mkdir()
    target = proj / "readme.txt"
    target.write_text("keep me\n")
    ctx = make_ctx(proj, [
        [[_spec("edit", path=str(target), old_string="keep me", new_string="changed")]],
        ["after"],
    ])
    vs = ctx.voice_session
    evs = []
    start_turn(ctx, vs, "edit it")
    async for ev in drain(vs):                                # drains up to the confirm
        evs.append(ev)
    assert evs[-1].type == "confirm"
    vs.turn_task.cancel()                                     # barge-in instead of confirming
    async for ev in drain(vs):                                # cancellation emits a final done
        evs.append(ev)
    assert any(e.type == "done" for e in evs)
    assert target.read_text() == "keep me\n"                  # edit never applied


# -- escalation de-stick (regression: stuck-on-Claude + re-announce every turn) --

def test_looks_simple():
    from gabagent.voice.turn import _looks_simple
    assert _looks_simple("stop")
    assert _looks_simple("pause the movie")
    assert _looks_simple("turn the volume up")
    assert not _looks_simple("can you see what is playing and unpause it")   # has 'and'
    assert not _looks_simple("please research the best approach to scaling this whole system")


async def test_escalation_announces_once_then_deescalates(home):
    proj = home / "proj"; proj.mkdir()
    ctx = make_ctx(proj, [["Sure."], ["Okay."], ["Stopped."]])
    ctx.config.router.enabled = True
    ctx.config.router.classifier_enabled = True
    ctx.config.router.complex_model = "claude-sonnet-4-5"
    classify_calls = []
    async def fake_classify(messages, model=None):
        classify_calls.append(1); return "[COMPLEX]"
    ctx.client.complete_simple = fake_classify

    evs1 = await run_turn(ctx, "please do a fairly complicated multi step thing for me right now")
    assert len([e for e in evs1 if e.type == "status" and "Claude" in e.text]) == 1   # announced once

    evs2 = await run_turn(ctx, "now do another complicated multi step thing for me as well please")
    assert [e for e in evs2 if e.type == "status" and "Claude" in e.text] == []        # no re-announce

    n = len(classify_calls)
    await run_turn(ctx, "stop")                       # short → fast-path
    assert len(classify_calls) == n                   # classifier NOT called for the simple turn
    assert ctx.active_model == ctx.config.router.simple_model   # de-escalated back to arya


# -- A1: one status per turn (no stacked "Opening… Trying window… Looking into it" preambles) -----

async def test_one_status_per_turn(home, monkeypatch):
    """A multi-batch turn (different tools → different status phrases) speaks ONE status, not one
    per batch."""
    from gabagent.api.models import ToolResult
    import gabagent.voice.turn as turn_mod
    async def fake_exec(tool_calls, *a, **k):
        return [ToolResult(output="ok") for _ in tool_calls]
    monkeypatch.setattr(turn_mod, "_execute_tool_calls", fake_exec)
    ctx = make_ctx(home, [
        [[_spec("read_file", path="x")]],     # phrase: "Reading through things."
        [[_spec("web_search", query="y")]],   # phrase: "Looking that up."  (different)
        ["all done"],
    ])
    evs = await run_turn(ctx, "do a couple of things")
    statuses = [e for e in evs if e.type == "status"]
    assert len(statuses) == 1                 # capped — without the fix this would be 2


# -- B1: turn-level arya fallback when an escalated turn fails to generate (G6) --------------------

class _FallbackClient:
    def __init__(self, fail_always=False, text_first=False):
        self.model = "arya"; self.calls = []
        self.fail_always = fail_always; self.text_first = text_first
    async def stream_complete(self, messages, tools=None, model=None, retry_model=None,
                              fallback_model=None, **kw):
        self.calls.append(model)
        if self.text_first:
            yield "partial "
        if self.fail_always or (model and model != "arya"):
            raise RuntimeError("APIError: The model failed to generate a response. "
                               "code: 'inference_failed'")
        yield "Recovered on arya."


async def test_escalated_inference_failure_falls_back_to_arya(home):
    ctx = make_ctx(home, [])
    ctx.client = _FallbackClient()
    ctx.active_model = "claude-sonnet-4-5"          # escalated for this turn
    evs = await run_turn(ctx, "a complex question")
    text = "".join(e.text for e in evs if e.type == "token")
    assert "Recovered on arya" in text              # answered, not errored
    assert ctx.client.calls == ["claude-sonnet-4-5", "arya"]   # retried once on the simple model
    assert not any(e.type == "error" for e in evs)


async def test_no_fallback_when_already_on_simple(home):
    """On the simple model already → no retry (nothing better to fall back to); surfaces the error."""
    ctx = make_ctx(home, [])
    ctx.client = _FallbackClient(fail_always=True)
    ctx.active_model = "arya"
    evs = await run_turn(ctx, "hi")
    assert ctx.client.calls == ["arya"]             # exactly one attempt, no fallback loop
    assert any(e.type == "error" for e in evs)


async def test_no_fallback_after_text_emitted(home):
    """If speech was already emitted, we must NOT replay on arya (no double-speak) — surface error."""
    ctx = make_ctx(home, [])
    ctx.client = _FallbackClient(text_first=True)
    ctx.active_model = "claude-sonnet-4-5"
    evs = await run_turn(ctx, "a complex question")
    # Content was already received (text_buf non-empty), so we must NOT replay on arya — surfaces the
    # error instead. (The speakable filter may still be buffering the partial, hence no token assert.)
    assert ctx.client.calls == ["claude-sonnet-4-5"]
    assert any(e.type == "error" for e in evs)


# -- C: shutdown honesty lives in the prompt -------------------------------------------------------

def test_addendum_has_shutdown_and_sleep_honesty():
    from gabagent.voice.turn import VOICE_ADDENDUM
    a = VOICE_ADDENDUM.lower()
    # Distinguishes shutdown from the sleep (mute/pause) escape hatch, and speaks first-person.
    assert "shut down voice mode" in a            # full stop
    assert "go to sleep" in a and "stop listening" in a   # mute/pause → sleep
    assert "first person" in a and "never 'yourself'" in a
    # And it knows it can't read images (screenshot honesty).
    assert "can't see or read images" in a or "can't look at it" in a
    # And it must verify current playback before claiming it (the "I'm playing My Mix 3" hallucination).
    assert "now_playing" in a and "from memory or assumption" in a
    # And it must not fabricate reasons for being slow (the "reconciling recent playback commands" excuse).
    assert "don't invent reasons for being slow" in a


class _EscTextThenFail:
    """Escalated model emits a token then dies (inference_failed) — the in-loop guard won't replay
    after speech, so the error reaches the OUTER handler with the model still escalated. arya's
    complete_simple then narrates. Mirrors the live escalate-after-tool failure shape."""
    def __init__(self): self.model = "arya"
    async def stream_complete(self, messages, tools=None, model=None, retry_model=None,
                              fallback_model=None, **kw):
        if model and model != "arya":
            yield "Uh "
            raise RuntimeError("APIError: The model failed to generate a response. "
                               "code: 'inference_failed'")
        yield "(unused)"
    async def complete_simple(self, messages, model=None):
        return "I've created the file for you."


async def test_escalation_failure_outer_belt_narrates_on_arya(home):
    ctx = make_ctx(home, [])
    ctx.client = _EscTextThenFail()
    ctx.active_model = "claude-sonnet-4-5"                 # escalated turn
    evs = await run_turn(ctx, "write a file")
    text = "".join(e.text for e in evs if e.type == "token")
    assert "created the file" in text                     # arya narrated via the outer belt
    assert not any(e.type == "error" for e in evs)        # no "[gab.ai error]" spoken


# -- Media-control keepalive: hold the wake window open for follow-ups while music plays -----------

class _FakeCatalog:
    """Minimal command catalog: `get(cid)` returns an object whose `.domain` is 'media' for the ids we
    declare media, else 'files' (so _is_media_command can discriminate)."""
    def __init__(self, media_ids):
        self._media = set(media_ids)
    def get(self, cid):
        if not cid:
            return None
        return types.SimpleNamespace(domain="media" if cid in self._media else "files")
    def index(self):
        return []   # empty → _capability_brief short-circuits (we only exercise .get here)


def _media_ctx(home, responses, **cfg_kw):
    from gabagent.api.models import ToolResult  # noqa: F401 (kept parallel to other tool tests)
    ctx = make_ctx(home, responses, **cfg_kw)
    ctx.command_catalog = _FakeCatalog({"jellyfin.control", "tidal.play"})
    return ctx


def test_keepalive_event_wire_shape():
    from gabagent.voice import events
    ev = events.keepalive(30)
    assert ev.type == "wake_hold"
    assert ev.to_dict() == {"type": "wake_hold", "hold": True, "ttl_secs": 30}


async def test_media_command_emits_keepalive_before_done(home, monkeypatch):
    """A media-domain run_command emits exactly one `wake_hold` (TTL = media_keepalive_secs) right
    before `done`, so the voice client holds the window open for a follow-up command."""
    from gabagent.api.models import ToolResult
    import gabagent.voice.turn as turn_mod
    async def fake_exec(tool_calls, *a, **k):
        return [ToolResult(output="Paused.") for _ in tool_calls]
    monkeypatch.setattr(turn_mod, "_execute_tool_calls", fake_exec)
    import gabagent.voice.addressed as _addr
    async def _yes(ctx, text):
        return True, "fast"
    monkeypatch.setattr(_addr, "is_addressed", _yes)
    ctx = _media_ctx(home, [
        [[_spec("run_command", command_id="jellyfin.control", action="pause")]],
        ["Paused."],
    ], media_keepalive_secs=30)
    evs = await run_turn(ctx, "pause the music")
    holds = [e for e in evs if e.type == "wake_hold"]
    assert len(holds) == 1
    assert holds[0].to_dict() == {"type": "wake_hold", "hold": True, "ttl_secs": 30}
    types_ = [e.type for e in evs]
    assert types_.index("wake_hold") < types_.index("done")   # before the turn closes
    assert types_[-1] == "done"


async def test_non_media_command_emits_no_keepalive(home, monkeypatch):
    """A non-media command (read_file) must NOT hold the wake window open."""
    from gabagent.api.models import ToolResult
    import gabagent.voice.turn as turn_mod
    async def fake_exec(tool_calls, *a, **k):
        return [ToolResult(output="ok") for _ in tool_calls]
    monkeypatch.setattr(turn_mod, "_execute_tool_calls", fake_exec)
    import gabagent.voice.addressed as _addr
    async def _yes(ctx, text):
        return True, "fast"
    monkeypatch.setattr(_addr, "is_addressed", _yes)
    ctx = _media_ctx(home, [
        [[_spec("run_command", command_id="system.something", action="x")]],
        ["done"],
    ], media_keepalive_secs=30)
    evs = await run_turn(ctx, "do a thing")
    assert not any(e.type == "wake_hold" for e in evs)


async def test_keepalive_disabled_when_secs_zero(home, monkeypatch):
    """media_keepalive_secs=0 disables the hold entirely, even for a media command."""
    from gabagent.api.models import ToolResult
    import gabagent.voice.turn as turn_mod
    async def fake_exec(tool_calls, *a, **k):
        return [ToolResult(output="Playing.") for _ in tool_calls]
    monkeypatch.setattr(turn_mod, "_execute_tool_calls", fake_exec)
    import gabagent.voice.addressed as _addr
    async def _yes(ctx, text):
        return True, "fast"
    monkeypatch.setattr(_addr, "is_addressed", _yes)
    ctx = _media_ctx(home, [
        [[_spec("run_command", command_id="tidal.play", uri="tidal:playlist:a")]],
        ["Playing."],
    ], media_keepalive_secs=0)
    evs = await run_turn(ctx, "play my playlist")
    assert not any(e.type == "wake_hold" for e in evs)
