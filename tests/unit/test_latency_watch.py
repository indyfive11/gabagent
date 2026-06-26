"""Latency self-test → auto-Turbo (#6) — the brain detects arya-dominated slowness, offers Turbo, toggles
on an affirmation, detects recovery, and offers off. All OFF unless voice_latency_watch."""
import types

import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.voice import latency_watch as L


@pytest.fixture(autouse=True)
def _reset():
    L._reset_for_test()
    yield
    L._reset_for_test()


def _ctx(watch=True, **over):
    cfg = GabAgentConfig(api_key="t", voice_latency_watch=watch,
                         voice_latency_ceiling_ms=8000, voice_latency_hard_ceiling_ms=15000,
                         voice_latency_floor_ms=4000, voice_latency_window=3,
                         voice_latency_offer_cooldown_secs=600.0, **over)
    return types.SimpleNamespace(config=cfg, session_id="s", turbo_commands=False, pending_turbo_offer=None)


# -- detection ---------------------------------------------------------------

def test_disabled_never_degrades():
    ctx = _ctx(watch=False)
    for _ in range(3):
        L.record(ctx, 20000, 20000)
    assert L.degraded(ctx) is False


def test_sustained_over_ceiling_degrades():
    ctx = _ctx()
    for _ in range(3):
        L.record(ctx, 9000, 9000)      # arya-dominated, > 8s ceiling
    assert L.degraded(ctx) is True


def test_single_hard_spike_degrades():
    ctx = _ctx()
    L.record(ctx, 3000, 3000)
    L.record(ctx, 16000, 16000)        # one arya-dominated turn past the 15s hard ceiling
    assert L.degraded(ctx) is True


def test_command_latency_does_not_degrade():
    """The Q4 case: 16s felt but arya was only 2s (a long Tidal call) → NOT arya-dominated → no trip."""
    ctx = _ctx()
    for _ in range(3):
        L.record(ctx, 2200, 16000)     # arya 2.2s of a 16s turn → ratio 0.14 < 0.6
    assert L.degraded(ctx) is False


def test_warm_turns_do_not_degrade():
    ctx = _ctx()
    for _ in range(3):
        L.record(ctx, 3500, 3500)
    assert L.degraded(ctx) is False


def test_recovered_needs_full_window_under_floor():
    ctx = _ctx()
    L.record(ctx, 3000, 3000)
    L.record(ctx, 3500, 3500)
    assert L.recovered(ctx) is False   # only 2 samples
    L.record(ctx, 3800, 3800)
    assert L.recovered(ctx) is True


# -- offer + affirmation -----------------------------------------------------

def test_offer_on_when_degraded_then_cooldown_blocks_renag():
    ctx = _ctx()
    for _ in range(3):
        L.record(ctx, 9000, 9000)
    o = L.maybe_offer(ctx, "s")
    assert o and o["kind"] == "turbo_on" and o["hold"] is True
    assert L.pending(ctx) is not None
    # a pending offer already exists → no second offer
    assert L.maybe_offer(ctx, "s") is None


def test_affirmation_toggles_turbo_on():
    ctx = _ctx()
    for _ in range(3):
        L.record(ctx, 9000, 9000)
    L.maybe_offer(ctx, "s")
    line = L.resolve_offer(ctx, "yes")
    assert line and "Turbo mode on" in line
    assert ctx.turbo_commands is True
    assert L.pending(ctx) is None


def test_non_affirmation_lapses_offer_and_does_not_toggle():
    ctx = _ctx()
    for _ in range(3):
        L.record(ctx, 9000, 9000)
    L.maybe_offer(ctx, "s")
    line = L.resolve_offer(ctx, "play some jazz")     # they moved on
    assert line is None
    assert ctx.turbo_commands is False
    assert L.pending(ctx) is None                      # offer dropped


def test_resolve_is_noop_without_pending():
    ctx = _ctx()
    assert L.resolve_offer(ctx, "yes") is None
    assert ctx.turbo_commands is False


def test_recovery_offers_off_only_in_turbo():
    ctx = _ctx()
    ctx.turbo_commands = True
    for _ in range(3):
        L.record(ctx, 3000, 3000)      # arya healthy again
    o = L.maybe_offer(ctx, "s")
    assert o and o["kind"] == "turbo_off"
    line = L.resolve_offer(ctx, "sure")
    assert line and "off" in line.lower()
    assert ctx.turbo_commands is False


def test_offer_ttl_expires(monkeypatch):
    ctx = _ctx(voice_latency_offer_ttl_secs=0.0)   # instant-expiry
    for _ in range(3):
        L.record(ctx, 9000, 9000)
    L.maybe_offer(ctx, "s")
    assert L.pending(ctx) is None                  # already expired


# -- integration through a real turn ----------------------------------------

from tests.unit.test_voice_turn import make_ctx, run_turn, FakeClient


class _StatsClient(FakeClient):
    """A client that publishes last_call_stats (ttft) like the real gab client, so the turn loop feeds the
    latency watcher."""
    def __init__(self, responses, ttft_ms):
        super().__init__(responses)
        self.last_call_stats = None
        self._ttft = ttft_ms

    async def stream_complete(self, messages, tools=None, model=None, retry_model=None, **kw):
        self.last_call_stats = {"ttft_ms": self._ttft, "total_ms": self._ttft}
        for c in self.responses.pop(0):
            yield c


async def test_turn_appends_offer_after_degraded(tmp_path):
    ctx = make_ctx(tmp_path, [], voice_latency_watch=True)
    ctx.client = _StatsClient([["Sure."], ["Sure."], ["Sure."]], ttft_ms=9000)
    L._reset_for_test()
    offered = False
    for _ in range(3):
        ctx.client.responses = ctx.client.responses or [["Sure."]]
        evs = await run_turn(ctx, "tell me something")
        if any(e.type == "token" and "Turbo mode" in e.text for e in evs):
            offered = True
    assert offered                                  # the offer appended once arya looked degraded
    assert any(e.type == "wake_hold" for e in evs)  # window held for the "yes"


async def test_turn_yes_toggles_turbo(tmp_path):
    ctx = make_ctx(tmp_path, [["ignored"]], voice_latency_watch=True)
    L._reset_for_test()
    # Arm a pending offer directly, then a bare "yes" must toggle Turbo with NO model call.
    L._set_pending(ctx, "turbo_on")
    evs = await run_turn(ctx, "yes")
    assert ctx.turbo_commands is True
    assert any(e.type == "token" and "Turbo mode on" in e.text for e in evs)
    assert ctx.client.responses == [["ignored"]]    # the canned model response was never consumed


# -- polish: anti-nag cooldown is per-direction; directional words aren't affirmations ----------

def test_declined_offer_cools_down_same_direction():
    ctx = _ctx()
    for _ in range(3):
        L.record(ctx, 9000, 9000)
    assert L.maybe_offer(ctx, "s") is not None      # offered ON
    L.resolve_offer(ctx, "no thanks")               # declined → pending cleared, cooldown set
    for _ in range(3):
        L.record(ctx, 9000, 9000)
    assert L.maybe_offer(ctx, "s") is None           # ON cooldown blocks the re-nag


def test_on_cooldown_does_not_block_off_offer():
    ctx = _ctx()
    for _ in range(3):
        L.record(ctx, 9000, 9000)
    L.maybe_offer(ctx, "s")                          # ON offered (sets ON cooldown)
    L.resolve_offer(ctx, "yes")                      # accept → in Turbo
    assert ctx.turbo_commands is True
    for _ in range(3):
        L.record(ctx, 3000, 3000)                    # recovered
    o = L.maybe_offer(ctx, "s")
    assert o and o["kind"] == "turbo_off"            # OFF not blocked by the recent ON cooldown


def test_recovery_offer_off_renag_blocked_after_decline():
    ctx = _ctx()
    ctx.turbo_commands = True
    for _ in range(3):
        L.record(ctx, 3000, 3000)
    assert L.maybe_offer(ctx, "s") is not None        # offered OFF
    L.resolve_offer(ctx, "not now")                   # declined
    for _ in range(3):
        L.record(ctx, 3000, 3000)
    assert L.maybe_offer(ctx, "s") is None            # OFF cooldown blocks the re-nag (was the bug)


def test_directional_phrase_is_not_an_affirmation():
    ctx = _ctx()
    ctx.turbo_commands = True
    for _ in range(3):
        L.record(ctx, 3000, 3000)
    L.maybe_offer(ctx, "s")                           # OFF offer pending
    # "turn it on" must NOT count as a yes to "switch Turbo off?" — it lapses the offer instead.
    assert L.resolve_offer(ctx, "turn it on") is None
    assert ctx.turbo_commands is True                 # unchanged


# -- polish R2: confirm-hold path (pre-duck) + explicit release on resolution -----------------

def _holds(evs):
    return [e.extra.get("hold") for e in evs if e.type == "wake_hold"]


async def test_offer_uses_confirm_hold_no_ttl(tmp_path):
    """The offer must emit wake_hold {hold:true} with NO ttl_secs → routes to the gate's pre-ducking
    _set_hold (so an over-media 'yes' isn't masked), not the keepalive path."""
    ctx = make_ctx(tmp_path, [], voice_latency_watch=True)
    ctx.client = _StatsClient([["Sure."]] * 4, ttft_ms=9000)
    L._reset_for_test()
    evs = []
    for _ in range(3):
        evs = await run_turn(ctx, "tell me something")
    holds = [e for e in evs if e.type == "wake_hold"]
    assert holds and holds[-1].extra.get("hold") is True
    assert "ttl_secs" not in holds[-1].extra            # no ttl → confirm-hold (pre-duck) path


async def test_accept_emits_release(tmp_path):
    ctx = make_ctx(tmp_path, [["ignored"]], voice_latency_watch=True)
    L._reset_for_test()
    L._set_pending(ctx, "turbo_on")
    evs = await run_turn(ctx, "yes")
    assert ctx.turbo_commands is True
    assert _holds(evs) == [False]                        # the confirm-hold is released on accept


async def test_lapsed_offer_emits_release(tmp_path):
    ctx = make_ctx(tmp_path, [["Sure."]], voice_latency_watch=True)
    L._reset_for_test()
    L._set_pending(ctx, "turbo_on")
    evs = await run_turn(ctx, "what time is it")           # not an affirmation → lapses
    assert ctx.turbo_commands is False
    assert False in _holds(evs)                            # held window released before normal handling
