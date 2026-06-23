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
    async def _aside(ctx, text, wake=None):
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


def test_bare_direction_pure():
    """The classifier matches ONLY a lone bare direction word, never an explicit terse command or a
    multi-token control phrase — so legit commands ('stop'/'pause'/'skip'/'next'/'mute'/'turn it up')
    pass through untouched."""
    from gabagent.voice.turn import _bare_direction
    assert _bare_direction("up") == "up"
    assert _bare_direction("  Up. ") == "up"          # punctuation/case/space normalized
    assert _bare_direction("OFF") == "off"
    for legit in ("stop", "pause", "skip", "next", "mute", "louder",
                  "turn it up", "volume up", "up up", "on the table", ""):
        assert _bare_direction(legit) == "", legit


async def test_bare_direction_guard_asks_and_runs_nothing(home, monkeypatch):
    """A lone ambiguous direction word ('up') — a classic garbled-STT fragment — must NOT auto-run a
    state-changing command. The brain asks ('Turn what up?') and ends the turn: no model call, no
    command, no history append. (Live 2026-06-23: bare 'up' auto-ran system.volume_up unasked.)"""
    import gabagent.voice.addressed as addr
    async def _addressed(ctx, text, wake=None):
        return True, "test"                            # reach the guard (it sits after the addressed gate)
    monkeypatch.setattr(addr, "is_addressed", _addressed)
    proj = home / "proj"; proj.mkdir()
    ctx = make_ctx(proj, [["should never speak"]])     # the model must never be called
    evs = await run_turn(ctx, "Up.")
    assert [e.type for e in evs] == ["token", "done"]
    assert next(e for e in evs if e.type == "token").text.lower() == "turn what up?"
    assert ctx.session.messages() == []                # the fragment never entered history


async def test_bare_direction_guard_off_lets_it_through(home, monkeypatch):
    """With the guard disabled, a bare 'up' falls through to normal routing (the model is reached) —
    proving the guard is the only thing short-circuiting it and the default is opt-out-able."""
    import gabagent.voice.addressed as addr
    async def _addressed(ctx, text, wake=None):
        return True, "test"
    monkeypatch.setattr(addr, "is_addressed", _addressed)
    proj = home / "proj"; proj.mkdir()
    ctx = make_ctx(proj, [["Okay."]], voice_bare_direction_guard=False)
    evs = await run_turn(ctx, "up")
    assert any(e.type == "token" and e.text == "Okay." for e in evs)   # reached the (fake) model
    assert ctx.session.messages()                                      # appended normally


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


async def test_first_token_latency_is_stamped_once(home, monkeypatch):
    # First-audio instrumentation (pairs with VAC's decomposed RESPONSE line): exactly one `first_token`
    # dlog per turn, carrying the brain's request-in → first-token-out delta in ms.
    import gabagent.voice.turn as turn_mod
    seen = []
    monkeypatch.setattr(turn_mod, "dlog", lambda ctx, event, **kw: seen.append((event, kw)))
    proj = home / "proj"
    proj.mkdir()
    ctx = make_ctx(proj, [["Two sentences here. And a second one."]])
    await run_turn(ctx, "say something")
    firsts = [(e, kw) for e, kw in seen if e == "first_token"]
    assert len(firsts) == 1                                   # stamped once, not per token
    assert "ms" in firsts[0][1] and isinstance(firsts[0][1]["ms"], int)


async def test_gab_call_dlog_emits_per_model_call(home, monkeypatch):
    # Phase-0 latency instrumentation: when the client exposes per-call stats (last_call_stats), the turn
    # loop dlogs one `gab_call` per model call, carrying the prefill/generation timing + token fields.
    import gabagent.voice.turn as turn_mod

    class _StatsClient(FakeClient):
        async def stream_complete(self, messages, tools=None, model=None, retry_model=None, **kw):
            chunks = self.responses.pop(0)
            for c in chunks:
                yield c
            self.last_call_stats = {"model": "arya", "ptoks": 1200, "ctoks": 30,
                                    "cached": 0, "ttft_ms": 800, "total_ms": 1500}

    seen = []
    monkeypatch.setattr(turn_mod, "dlog", lambda ctx, event, **kw: seen.append((event, kw)))
    proj = home / "proj"
    proj.mkdir()
    ctx = make_ctx(proj, [["A short reply."]])
    ctx.client = _StatsClient([["A short reply."]])           # _active_client(gab) returns ctx.client
    await run_turn(ctx, "say something")
    calls = [kw for e, kw in seen if e == "gab_call"]
    assert len(calls) == 1                                     # one per model call this single-round turn
    assert calls[0]["ptoks"] == 1200 and calls[0]["ttft_ms"] == 800
    assert calls[0]["cached"] == 0 and "round" in calls[0]


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
    # B(ii) widen (6→10 words): ordinary single-clause conversational/factual turns up to 10 words now
    # skip the classify instead of paying the ~2.3s arya round-trip.
    assert _looks_simple("what's the weather like in seattle this weekend")          # 8w, simple
    assert _looks_simple("who played the joker in the first batman movie")          # 10w, simple
    # ...but the two guards keep genuinely-complex turns ON the classify→escalate path:
    assert not _looks_simple("explain how tcp differs from udp in networking")       # reasoning marker
    assert not _looks_simple("what's the best way to structure this project")        # depth marker
    assert not _looks_simple("play some jazz then turn the lights down")             # compound 'then'
    assert not _looks_simple("tell me a long detailed story about a dragon and a knight in a castle")  # >10w


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
    evs = await run_turn(ctx, "play something")      # a real command (a bare "hi" is now wake-only → silent)
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


# -- loop detector: a stuck tool loop escalates a rung, then bails honestly ------------------------

def test_tool_sig_counts_repetition_by_result_not_just_command_id():
    """Live 2026-06-20: the Foreigner turn (play→ask, play→ask, list, play-by-uri→SUCCESS) tripped the
    detector by command_id alone and told the user it failed right after the playlist started. The
    signature must fold in the RESULT so progress reads as progress, while an identical no-op repeat
    (the 13x "Resuming.") still collapses to one signature."""
    from gabagent.voice.turn import _tool_sig
    play = _spec("run_command", command_id="tidal.play")
    ask = types.SimpleNamespace(output="I have more than one playlist — did you mean…?", error=None, success=True)
    ok = types.SimpleNamespace(output="Playing that playlist on TIDAL.", error=None, success=True)
    resume = types.SimpleNamespace(output="Resuming.", error=None, success=True)
    assert _tool_sig(play, ask) == _tool_sig(play, ask)        # same call, same response → stuck signal
    assert _tool_sig(play, ask) != _tool_sig(play, ok)         # progress (different result) → distinct
    assert _tool_sig(play, resume) == _tool_sig(play, resume)  # identical no-op still collapses (caught)


# -- loop detector: stuck-loop bail + sequential escalation ----------------------------------------

async def test_tool_loop_bails_with_honest_line(home):
    """The live 2026-06-20 failure: the model fired tidal.play 13× in one turn, each a no-op, and the
    user heard nothing. With no ladder to climb (router off), the loop guard must STOP after the repeat
    threshold and speak an honest line instead of hammering the same call forever."""
    proj = home / "proj"; proj.mkdir()
    one = [[_spec("read_file", path="nope.txt")]]           # one tool-call round (chunk = list of specs)
    ctx = make_ctx(proj, [one, one, one])                  # 3 identical rounds → stuck → bail (no climb)
    evs = await run_turn(ctx, "read that file over and over")
    spoken = "".join(e.text for e in evs if e.type == "token")
    assert "trouble" in spoken.lower()                     # the honest give-up line, not silence
    assert evs[-1].type == "done"                          # turn ended cleanly
    assert ctx.client.responses == []                      # consumed exactly the 3 — did not run away


async def test_command_loop_escalates_one_rung_then_recovers(home):
    """A command-intent turn that loops on the floor climbs ONE rung (least-escalation-first) to a more
    tool-capable model, which then breaks the loop. Proves the dead command-escalation path is wired to
    the sequential climb above whatever the floor is."""
    proj = home / "proj"; proj.mkdir()
    play = [[_spec("run_command", command_id="tidal.play", playlist="Retro Favorites")]]
    ctx = make_ctx(proj, [
        play, play, play,                                  # 3 identical rounds → stuck → climb one rung
        ["Got your Retro Favorites playlist going."],      # the higher rung breaks the loop with a reply
    ])
    # Enable the assembled cross-backend ladder (gab floor → claude rungs) and serve every backend the
    # same fake client so the climb is observable without real network.
    ctx.config.router.enabled = True
    ctx.config.router.cross_backend = True
    ctx.config.claude.api_key = "sk-test"
    ctx.clients = {"gab": ctx.client, "claude": ctx.client}
    evs = await run_turn(ctx, "play my retro favorites playlist")   # looks_like_command → command_intent
    assert ctx.active_backend == "claude"                  # climbed off the arya floor
    assert ctx.active_model != "arya"                      # onto a higher, tool-capable rung
    spoken = "".join(e.text for e in evs if e.type == "token")
    assert "retro favorites" in spoken.lower()             # the higher rung broke the loop and answered


# -- internet-outage failover to the local model ---------------------------------------------------

import httpx


class _OutageClient:
    """Cloud client that's unreachable — every call raises a connectivity error (internet down)."""
    def __init__(self, exc=None):
        self.model = "arya"; self.calls = 0
        self._exc = exc or httpx.ConnectError("Temporary failure in name resolution")
    async def stream_complete(self, messages, tools=None, model=None, **kw):
        self.calls += 1
        raise self._exc
        yield ""   # noqa: unreachable — makes this an async generator
    async def complete_simple(self, messages, model=None, **kw):
        raise self._exc


def test_is_connectivity_error_distinguishes_outage_from_server_errors():
    from gabagent.api.client import _is_connectivity_error as C
    import openai
    assert C(httpx.ConnectError("Connection refused")) is True
    assert C(Exception("Temporary failure in name resolution")) is True
    assert C(openai.APIConnectionError(request=httpx.Request("GET", "http://x"))) is True
    # A server that ANSWERED (any HTTP status) means the internet is up → NOT a connectivity error.
    assert C(RuntimeError("code: 402 insufficient credits")) is False
    assert C(RuntimeError("code: 401 invalid api key")) is False
    assert C(ValueError("unrelated bug")) is False


async def test_outage_fails_over_to_local_and_answers(home, monkeypatch):
    """A cloud connectivity failure auto-switches to the on-demand local model: speaks the offline notice,
    answers on local, sets local_mode + offline_failover, and never surfaces an error."""
    import gabagent.local.ollama as ol
    async def _ok(ctx): return None
    monkeypatch.setattr(ol, "ensure_ollama_running", _ok)        # don't actually start Ollama
    proj = home / "proj"; proj.mkdir()
    ctx = make_ctx(proj, [], local_model="devstral:24b", voice_intent_filter=False)
    ctx.client = _OutageClient()                                  # cloud is down
    ctx.local_client = FakeClient([["Paris is the capital of France."]])
    evs = await run_turn(ctx, "what's the capital of France")
    spoken = "".join(e.text for e in evs if e.type == "token").lower()
    assert "local brain" in spoken                               # the offline transition notice
    assert "paris" in spoken                                     # answered, on the local model
    assert ctx.local_mode and ctx.offline_failover               # latched into offline mode
    assert not any(e.type == "error" for e in evs)               # no "say that again" error
    assert ctx.client.calls == 1                                 # tried cloud once, then gave up on it


async def test_billing_error_does_not_fail_over(home, monkeypatch):
    """A 402/billing (server ANSWERED) is not a connectivity outage — it must NOT trigger offline
    failover (that path is for a dead network only); it surfaces as before."""
    import gabagent.local.ollama as ol
    async def _ok(ctx): return None
    monkeypatch.setattr(ol, "ensure_ollama_running", _ok)
    proj = home / "proj"; proj.mkdir()
    ctx = make_ctx(proj, [], local_model="devstral:24b", voice_intent_filter=False)
    ctx.client = _OutageClient(exc=RuntimeError("code: 402 insufficient credits"))
    evs = await run_turn(ctx, "what's the capital of France")
    spoken = "".join(e.text for e in evs if e.type == "token").lower()
    assert not ctx.local_mode and not ctx.offline_failover       # stayed on cloud
    assert "local brain" not in spoken                           # no offline failover


async def test_offline_failover_kill_switch_disables_it(home, monkeypatch):
    """voice_offline_failover=False (or no local_model) ⇒ a connectivity error surfaces as the normal
    hiccup, no switch to local."""
    import gabagent.local.ollama as ol
    async def _ok(ctx): return None
    monkeypatch.setattr(ol, "ensure_ollama_running", _ok)
    proj = home / "proj"; proj.mkdir()
    ctx = make_ctx(proj, [], local_model="devstral:24b", voice_intent_filter=False,
                   voice_offline_failover=False)
    ctx.client = _OutageClient()
    evs = await run_turn(ctx, "what's the capital of France")
    assert not ctx.local_mode                                    # disabled → no failover
    assert any(e.type == "error" for e in evs)


async def test_recovery_probe_reverts_to_cloud_when_back(home, monkeypatch):
    """While offline-failed-over, the per-turn probe checks the cloud at the top of the turn; when it's
    reachable again, revert to the cloud router, say 'back online', and answer on the cloud model."""
    import gabagent.voice.turn as turnmod
    import gabagent.local.ollama as ol
    async def _reachable(ctx): return True
    async def _noop(ctx): return None
    monkeypatch.setattr(turnmod, "_cloud_reachable", _reachable)  # cloud is back
    monkeypatch.setattr(ol, "unload_local", _noop)               # don't hit a real Ollama to free VRAM
    proj = home / "proj"; proj.mkdir()
    ctx = make_ctx(proj, [["Welcome back — 2 plus 2 is 4."]], voice_intent_filter=False,
                   local_model="devstral:24b")
    ctx.local_mode = True; ctx.offline_failover = True           # pretend a prior turn failed over
    ctx.local_client = FakeClient([["should not be used"]])
    evs = await run_turn(ctx, "what is two plus two")
    spoken = "".join(e.text for e in evs if e.type == "token").lower()
    assert "back online" in spoken                               # the recovery notice
    assert "4" in spoken                                         # answered on the cloud model
    assert not ctx.local_mode and not ctx.offline_failover       # reverted cleanly


def test_offline_addendum_only_present_when_failed_over(home):
    """The 'you are offline' system-prompt guidance appears only while offline_failover is set, so a
    normal online turn is unchanged."""
    from gabagent.voice.turn import _voice_system
    proj = home / "proj"; proj.mkdir()
    ctx = make_ctx(proj, [])
    assert "the internet is down" not in _voice_system(ctx).lower()
    ctx.offline_failover = True
    assert "the internet is down" in _voice_system(ctx).lower()


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


def test_addendum_drops_greeting_welded_to_a_command():
    # Item C compound case: STT welds a phantom "how are you?" in front of a real command
    # ("Hey, how are you? Play my retro favorites") — the model must skip the social reply and
    # just do the request, NOT prepend "I'm doing well". (Pairs with the acoustic wake-signal,
    # which can only suppress a WHOLE turn and so can't strip a greeting off a command turn.)
    from gabagent.voice.turn import VOICE_ADDENDUM
    a = VOICE_ADDENDUM.lower()
    # A greeting fused in front of a command/question is treated as a mis-hear, not answered.
    assert "skip the social reply" in a
    assert "mis-hear" in a
    # The single-rule carve-out: only answer "how are you" when it's the whole utterance.
    assert "whole utterance with nothing to act on" in a


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
    async def _yes(ctx, text, wake=None):
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
    async def _yes(ctx, text, wake=None):
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
    async def _yes(ctx, text, wake=None):
        return True, "fast"
    monkeypatch.setattr(_addr, "is_addressed", _yes)
    ctx = _media_ctx(home, [
        [[_spec("run_command", command_id="tidal.play", uri="tidal:playlist:a")]],
        ["Playing."],
    ], media_keepalive_secs=0)
    evs = await run_turn(ctx, "play my playlist")
    assert not any(e.type == "wake_hold" for e in evs)


# -- Terminal-command confirmation: speak the tool output, skip the narration model call -------------

class _TermCatalog:
    """Catalog whose `get(cid)` returns an object carrying `.domain` and `.terminal_confirm` so the turn
    can exercise the terminal-confirm short-circuit."""
    def __init__(self, terminal):
        self._terminal = set(terminal)
    def get(self, cid):
        if not cid:
            return None
        return types.SimpleNamespace(domain="media", terminal_confirm=cid in self._terminal)
    def index(self):
        return []


async def test_terminal_confirm_command_skips_narration(home, monkeypatch):
    """A terminal_confirm command speaks its own tool output and ENDS the turn — the follow-up narration
    model call is skipped (the 2nd canned response is never consumed)."""
    from gabagent.api.models import ToolResult
    import gabagent.voice.turn as turn_mod
    async def fake_exec(tool_calls, *a, **k):
        return [ToolResult(output="Skipped ahead 30 seconds.") for _ in tool_calls]
    monkeypatch.setattr(turn_mod, "_execute_tool_calls", fake_exec)
    import gabagent.voice.addressed as _addr
    async def _yes(ctx, text, wake=None):
        return True, "fast"
    monkeypatch.setattr(_addr, "is_addressed", _yes)
    seen = []
    monkeypatch.setattr(turn_mod, "dlog", lambda ctx, e, **k: seen.append((e, k)))
    ctx = make_ctx(home, [
        [[_spec("run_command", command_id="jellyfin.control", action="forward")]],
        ["NARRATION THAT MUST NOT BE SPOKEN"],
    ])
    ctx.command_catalog = _TermCatalog(terminal={"jellyfin.control"})
    evs = await run_turn(ctx, "skip ahead")
    spoken = "".join(e.text for e in evs if e.type == "token")
    assert "Skipped ahead 30 seconds." in spoken
    assert "NARRATION" not in spoken                       # narration round skipped
    assert len(ctx.client.responses) == 1                  # 2nd response never consumed → one model call
    assert any(e == "terminal_confirm" for e, _ in seen)


async def test_non_terminal_command_still_narrates(home, monkeypatch):
    """A command WITHOUT terminal_confirm still goes through the model narration round (2nd call), so its
    structured/query result gets phrased — the short-circuit must not fire."""
    from gabagent.api.models import ToolResult
    import gabagent.voice.turn as turn_mod
    async def fake_exec(tool_calls, *a, **k):
        return [ToolResult(output='[{"title":"Heat"}]') for _ in tool_calls]
    monkeypatch.setattr(turn_mod, "_execute_tool_calls", fake_exec)
    import gabagent.voice.addressed as _addr
    async def _yes(ctx, text, wake=None):
        return True, "fast"
    monkeypatch.setattr(_addr, "is_addressed", _yes)
    ctx = make_ctx(home, [
        [[_spec("run_command", command_id="jellyfin.search", query="heat")]],
        ["I found Heat."],
    ])
    ctx.command_catalog = _TermCatalog(terminal={"jellyfin.control"})   # search is NOT terminal
    evs = await run_turn(ctx, "search for heat")
    spoken = "".join(e.text for e in evs if e.type == "token")
    assert "I found Heat." in spoken                        # narration round ran
    assert len(ctx.client.responses) == 0                  # both responses consumed → two model calls


async def test_terminal_confirm_skipped_on_tool_error(home, monkeypatch):
    """A FAILED terminal command must NOT short-circuit — it falls through to model narration so the
    failure is phrased naturally instead of speaking a raw/empty output."""
    from gabagent.api.models import ToolResult
    import gabagent.voice.turn as turn_mod
    async def fake_exec(tool_calls, *a, **k):
        return [ToolResult(output="", error="control failed") for _ in tool_calls]
    monkeypatch.setattr(turn_mod, "_execute_tool_calls", fake_exec)
    import gabagent.voice.addressed as _addr
    async def _yes(ctx, text, wake=None):
        return True, "fast"
    monkeypatch.setattr(_addr, "is_addressed", _yes)
    ctx = make_ctx(home, [
        [[_spec("run_command", command_id="jellyfin.control", action="forward")]],
        ["Sorry, I couldn't do that."],
    ])
    ctx.command_catalog = _TermCatalog(terminal={"jellyfin.control"})
    evs = await run_turn(ctx, "skip ahead")
    spoken = "".join(e.text for e in evs if e.type == "token")
    assert "Sorry, I couldn't do that." in spoken          # error narrated by the model, not short-circuited
    assert len(ctx.client.responses) == 0                  # both consumed → narration ran


# -- convo_hold: release the voice conversation-hold on a TERMINAL one-shot reply (VAC Phase-2 ask) --

def test_convo_hold_event_wire_shape():
    from gabagent.voice import events
    ev = events.convo_hold()
    assert ev.type == "convo_hold"
    assert ev.to_dict() == {"type": "convo_hold", "release": True}   # arrival-keyed; release present


def test_is_terminal_reply_heuristic():
    from gabagent.voice.turn import _is_terminal_reply
    assert _is_terminal_reply("It's three o'clock.") is True          # self-contained answer
    assert _is_terminal_reply("You're welcome.") is True              # acknowledgement
    assert _is_terminal_reply("Which playlist did you mean?") is False  # awaiting an answer
    assert _is_terminal_reply('Did you mean "Jaymes"?') is False      # trailing quote stripped, still a Q
    assert _is_terminal_reply("") is False                            # nothing said → nothing to release
    # A long declarative reply (a story) IS terminal — the bed restores promptly at BotStopped instead of
    # lingering through the ~15s convo-hold tail (live-drive #3: Rob wants the music back right after she
    # finishes). bot_speaking handles the mid-narration watchdog, so length doesn't affect terminality.
    story = ("Frisha the cat had decided that Diamond the dog was the dumbest creature she had ever met, "
             "and yet when the thunderstorm hit at two in the morning it was Diamond who sat beside her "
             "without being asked, warm and steady and entirely unbothered by the noise outside.")
    assert _is_terminal_reply(story) is True


async def test_terminal_qa_emits_convo_hold_before_done(home, monkeypatch):
    """A terminal one-shot reply (no tools, not a question) emits exactly one `convo_hold` right before
    `done`, so the voice side can drop the bed-duck early instead of holding the full window."""
    import gabagent.voice.addressed as _addr
    async def _yes(ctx, text, wake=None):
        return True, "fast"
    monkeypatch.setattr(_addr, "is_addressed", _yes)
    ctx = make_ctx(home, [["It's three o'clock."]])
    evs = await run_turn(ctx, "what time is it")
    holds = [e for e in evs if e.type == "convo_hold"]
    assert len(holds) == 1
    assert holds[0].to_dict() == {"type": "convo_hold", "release": True}
    types_ = [e.type for e in evs]
    assert types_.index("convo_hold") < types_.index("done")
    assert types_[-1] == "done"


async def test_question_reply_does_not_emit_convo_hold(home, monkeypatch):
    """A reply that ends in a question is NOT terminal — keep the hold open for the user's answer."""
    import gabagent.voice.addressed as _addr
    async def _yes(ctx, text, wake=None):
        return True, "fast"
    monkeypatch.setattr(_addr, "is_addressed", _yes)
    ctx = make_ctx(home, [["Which playlist did you mean?"]])
    evs = await run_turn(ctx, "play my playlist")
    assert not any(e.type == "convo_hold" for e in evs)


async def test_terminal_media_turn_emits_both_keepalive_and_convo_hold(home, monkeypatch):
    """A TERMINAL media-control turn emits BOTH the keepalive (wake_hold — keeps the MIC window open to chain
    'louder'/'skip' without re-waking) AND convo_hold release (restores the BED promptly at BotStopped). The
    two are independent on the voice side, so a 'volume up' confirmation gives the music back right away
    without losing chaining (live-drive #3 ask)."""
    from gabagent.api.models import ToolResult
    import gabagent.voice.turn as turn_mod
    async def fake_exec(tool_calls, *a, **k):
        return [ToolResult(output="Paused.") for _ in tool_calls]
    monkeypatch.setattr(turn_mod, "_execute_tool_calls", fake_exec)
    import gabagent.voice.addressed as _addr
    async def _yes(ctx, text, wake=None):
        return True, "fast"
    monkeypatch.setattr(_addr, "is_addressed", _yes)
    ctx = _media_ctx(home, [
        [[_spec("run_command", command_id="jellyfin.control", action="pause")]],
        ["Paused the music for you."],
    ], media_keepalive_secs=30)
    evs = await run_turn(ctx, "pause the music")
    assert any(e.type == "convo_hold" for e in evs)       # terminal reply → bed restores promptly
    assert any(e.type == "wake_hold" for e in evs)        # the media keepalive still holds the mic open


async def test_media_turn_ending_in_question_keeps_the_hold(home, monkeypatch):
    """A media turn whose reply ENDS in a question (a clarification) is NOT terminal — no convo_hold release,
    so the bed stays ducked while the voice side waits for Rob's answer. The keepalive still fires."""
    from gabagent.api.models import ToolResult
    import gabagent.voice.turn as turn_mod
    async def fake_exec(tool_calls, *a, **k):
        return [ToolResult(output="more than one match") for _ in tool_calls]
    monkeypatch.setattr(turn_mod, "_execute_tool_calls", fake_exec)
    import gabagent.voice.addressed as _addr
    async def _yes(ctx, text, wake=None):
        return True, "fast"
    monkeypatch.setattr(_addr, "is_addressed", _yes)
    ctx = _media_ctx(home, [
        [[_spec("run_command", command_id="tidal.play", playlist="x")]],
        ["Did you mean Jaymes or Retro Favorites?"],
    ], media_keepalive_secs=30)
    evs = await run_turn(ctx, "play my playlist")
    assert not any(e.type == "convo_hold" for e in evs)   # question reply → hold stays open for the answer
    assert any(e.type == "wake_hold" for e in evs)


async def test_convo_hold_disabled_by_flag(home, monkeypatch):
    """voice_convo_hold_release=False suppresses the hint entirely (degrades to the voice-side timer)."""
    import gabagent.voice.addressed as _addr
    async def _yes(ctx, text, wake=None):
        return True, "fast"
    monkeypatch.setattr(_addr, "is_addressed", _yes)
    ctx = make_ctx(home, [["It's three o'clock."]], voice_convo_hold_release=False)
    evs = await run_turn(ctx, "what time is it")
    assert not any(e.type == "convo_hold" for e in evs)


# -- voice_volume: F3 my-voice-volume control event (wire shape co-designed with the voice agent) --

async def test_voice_set_volume_emits_voice_volume_before_done(home, monkeypatch):
    """voice.set_volume records a my-voice-volume request; the turn emits exactly one `voice_volume`
    event (carrying op) right before `done` for the voice side to map onto its TTS gain."""
    from gabagent.commands.providers import voice_control as vc
    import gabagent.voice.turn as turn_mod
    async def fake_exec(tool_calls, ctx, *a, **k):
        # Mirror real dispatch: run the backend so it sets ctx._voice_volume_signal.
        return [await vc.set_volume(ctx, op="down") for _ in tool_calls]
    monkeypatch.setattr(turn_mod, "_execute_tool_calls", fake_exec)
    import gabagent.voice.addressed as _addr
    async def _yes(ctx, text, wake=None):
        return True, "fast"
    monkeypatch.setattr(_addr, "is_addressed", _yes)
    ctx = _media_ctx(home, [
        [[_spec("run_command", command_id="voice.set_volume", op="down")]],
        ["Okay, lowering my voice."],
    ])
    evs = await run_turn(ctx, "lower your voice")
    vv = [e for e in evs if e.type == "voice_volume"]
    assert len(vv) == 1
    assert vv[0].to_dict() == {"type": "voice_volume", "op": "down"}
    types_ = [e.type for e in evs]
    assert types_.index("voice_volume") < types_.index("done")
    assert types_[-1] == "done"


async def test_voice_volume_kill_switch_suppresses_event(home, monkeypatch):
    """voice_volume_control=False: the command still runs and speaks, but NO event crosses the wire."""
    from gabagent.commands.providers import voice_control as vc
    import gabagent.voice.turn as turn_mod
    async def fake_exec(tool_calls, ctx, *a, **k):
        return [await vc.set_volume(ctx, op="up") for _ in tool_calls]
    monkeypatch.setattr(turn_mod, "_execute_tool_calls", fake_exec)
    import gabagent.voice.addressed as _addr
    async def _yes(ctx, text, wake=None):
        return True, "fast"
    monkeypatch.setattr(_addr, "is_addressed", _yes)
    ctx = _media_ctx(home, [
        [[_spec("run_command", command_id="voice.set_volume", op="up")]],
        ["Okay, speaking up."],
    ], voice_volume_control=False)
    evs = await run_turn(ctx, "speak up")
    assert not any(e.type == "voice_volume" for e in evs)


# -- Turbo Mode: hybrid routing (commands → fast rung, conversation → normal), voice-toggled -----------

def test_turbo_meta_command_detection():
    """'turbo mode' family toggles ON, 'regular/normal mode' family toggles OFF, and media controls
    (fast forward / skip) must NEVER be read as a turbo toggle."""
    from gabagent.voice.commands import detect_meta_command as d
    for phrase in ("go to turbo mode", "turbo mode", "turn on turbo", "speed it up", "turbo on"):
        mc = d(phrase)
        assert mc is not None and mc.kind == "turbo" and mc.value == "on", phrase
    for phrase in ("regular mode", "normal mode", "turbo off", "exit turbo", "back to regular mode"):
        mc = d(phrase)
        assert mc is not None and mc.kind == "turbo" and mc.value == "off", phrase
    for phrase in ("fast forward", "skip ahead", "pause the movie", "turn it up", "play some music"):
        mc = d(phrase)
        assert mc is None or mc.kind != "turbo", phrase


def test_turbo_rung_picks_claude_floor_else_none():
    """_turbo_rung returns the first Claude rung of an assembled ladder (the fast haiku floor), or None
    when no Claude backend is available (Turbo then no-ops)."""
    from gabagent.agent.router import ModelRouter
    from gabagent.voice.turn import _turbo_rung
    cfg = GabAgentConfig(api_key="gabkey")
    cfg.claude.api_key = "ankey"
    cfg.router.cross_backend = True
    r = ModelRouter.assemble(cfg, local_floor=False, local_running=False)
    tr = _turbo_rung(r)
    assert tr is not None and tr.backend == "claude"
    assert tr.model == cfg.claude.ladder[0].model          # the Claude floor (haiku)
    # No Anthropic key → no Claude rung → None
    cfg2 = GabAgentConfig(api_key="gabkey")
    cfg2.claude.api_key = ""
    r2 = ModelRouter.assemble(cfg2, local_floor=False, local_running=False)
    assert _turbo_rung(r2) is None
    assert _turbo_rung(None) is None


async def test_turbo_toggle_sets_flag_and_confirms(home, monkeypatch):
    """Saying 'turbo mode' flips ctx.turbo_commands on with a spoken confirm and no model call; 'regular
    mode' flips it back."""
    proj = home / "proj"; proj.mkdir()
    ctx = make_ctx(proj, [["MODEL SHOULD NOT BE CALLED"]])
    ctx.config.claude.api_key = "ankey"
    ctx.config.router.cross_backend = True
    evs = await run_turn(ctx, "go to turbo mode")
    assert ctx.turbo_commands is True
    assert any(e.type == "token" and "Turbo mode on" in e.text for e in evs)
    assert len(ctx.client.responses) == 1                  # FakeClient untouched — no model call
    evs2 = await run_turn(ctx, "regular mode")
    assert ctx.turbo_commands is False
    assert any(e.type == "token" and "regular mode" in e.text.lower() for e in evs2)


async def test_turbo_routes_command_to_claude_and_skips_classify(home, monkeypatch):
    """With Turbo on, a command-intent turn routes straight to the Claude fast rung and SKIPS the arya
    classify (route via=turbo). A conversation turn is unaffected (normal routing)."""
    import gabagent.voice.turn as turn_mod
    from gabagent.agent.router import ModelRouter
    # Spy: record any classify call so we can assert Turbo skipped it.
    calls = {"classify": 0}
    orig = ModelRouter.classify_rung
    async def spy_classify(self, *a, **k):
        calls["classify"] += 1
        return 0
    monkeypatch.setattr(ModelRouter, "classify_rung", spy_classify)
    seen = []
    monkeypatch.setattr(turn_mod, "dlog", lambda ctx, e, **k: seen.append((e, k)))
    import gabagent.voice.addressed as _addr
    async def _yes(ctx, text, wake=None):
        return True, "fast"
    monkeypatch.setattr(_addr, "is_addressed", _yes)

    proj = home / "proj"; proj.mkdir()
    ctx = make_ctx(proj, [["should not be used (gab)"]])
    ctx.config.router.enabled = True
    ctx.config.claude.api_key = "ankey"
    ctx.config.router.cross_backend = True
    ctx.turbo_commands = True
    ctx.clients = {"claude": FakeClient([["Paused."]])}    # the turbo rung's client answers the turn

    await run_turn(ctx, "pause the music")                 # command-intent
    routes = [kw for e, kw in seen if e == "route"]
    assert any(kw.get("via") == "turbo" and kw.get("backend") == "claude" for kw in routes)
    assert calls["classify"] == 0                          # classify skipped on the turbo command turn
    assert ctx.active_backend == "claude"
