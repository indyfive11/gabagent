"""G2 timers — brain-solo core. Faked-clock unit tests (no server, no real wall clock except
where the ticker would read it; all logic takes an injectable `now`)."""
import types

import pytest

from gabagent.voice import timers as t
from gabagent.voice import events
from gabagent.voice.session import VoiceSession
from gabagent.commands.providers.timer import (
    PROVIDER, set_timer, list_timers, cancel_timer,
)


def _ctx(vs):
    # backends only touch ctx.voice_session; dlog no-ops without voice_debug_path.
    return types.SimpleNamespace(voice_session=vs, voice_debug_path=None)


# -- pure helpers -----------------------------------------------------------

@pytest.mark.parametrize("text,secs", [
    ("10 minutes", 600),
    ("10 min", 600),
    ("5m", 300),
    ("90 seconds", 90),
    ("90s", 90),
    ("1 hour", 3600),
    ("1h", 3600),
    ("1h30m", 5400),
    ("2 min 30 s", 150),
    ("1 hour 5 minutes 10 seconds", 3910),
    ("", None),
    ("soon", None),
    (None, None),
])
def test_parse_duration(text, secs):
    assert t.parse_duration(text) == secs


@pytest.mark.parametrize("secs,phrase", [
    (30, "30 seconds"),
    (60, "1 minute"),
    (600, "10 minutes"),
    (3600, "1 hour"),
    (3660, "1 hour 1 minute"),
    (3905, "1 hour 5 minutes"),   # seconds dropped when hours present
    (0, "0 seconds"),
])
def test_human(secs, phrase):
    assert t.human(secs) == phrase


def test_join_phrase():
    assert t.join_phrase([]) == ""
    assert t.join_phrase(["a"]) == "a"
    assert t.join_phrase(["a", "b"]) == "a and b"
    assert t.join_phrase(["a", "b", "c"]) == "a, b, and c"


# -- ring phrasing + proactive-channel delivery -----------------------------

def test_ring_phrase():
    vs = VoiceSession("s", None)
    labelled = t.add_timer(vs, 600, "pasta", now=0.0)
    bare = t.add_timer(vs, 90, None, now=0.0)
    assert t.ring_phrase(labelled) == "Your pasta timer is up — that was 10 minutes."
    assert t.ring_phrase(bare) == "Your 1 minute 30 seconds timer is up."


async def test_fire_due_enqueues_ring_onto_announce_channel(tmp_path, monkeypatch):
    # A fired timer now lands a spoken ring on the deferred-announce channel (the phase-2 unblock), keyed
    # to its owning session for originating-first delivery.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    from gabagent.voice import announce_store
    vs = VoiceSession("kitchen", None)
    t.add_timer(vs, 60, "eggs", now=0.0)
    sessions = {"kitchen": vs}
    fired = await t.fire_due(_ctx(vs), sessions, now=100.0)
    assert [x.label for x in fired] == ["eggs"]
    # The originating session drains it; a foreign session within the grace does not.
    assert announce_store.poll("office", now=101.0, lease_secs=5.0) == []
    out = announce_store.poll("kitchen", now=101.0, lease_secs=5.0)
    assert len(out) == 1 and "eggs timer is up" in out[0]["text"]


# -- state helpers ----------------------------------------------------------

def test_add_timer_unique_ids_and_expiry():
    vs = VoiceSession("s", None)
    a = t.add_timer(vs, 600, "pasta", now=1000.0)
    b = t.add_timer(vs, 60, None, now=1000.0)
    assert a.id != b.id
    assert a.expires_epoch == 1600.0 and b.expires_epoch == 1060.0
    assert {x.id for x in t.active(vs)} == {a.id, b.id}
    # active() is soonest-first
    assert t.active(vs)[0].id == b.id


def test_remaining_secs_ceils():
    vs = VoiceSession("s", None)
    tm = t.add_timer(vs, 600, now=1000.0)
    assert t.remaining_secs(tm, now=1000.5) == 600   # ceil(599.5)
    assert t.remaining_secs(tm, now=1599.1) == 1
    assert t.remaining_secs(tm, now=1600.0) == 0
    assert t.remaining_secs(tm, now=1700.0) == 0     # never negative


def test_pop_due():
    vs = VoiceSession("s", None)
    t.add_timer(vs, 10, "a", now=0.0)    # due at 10
    t.add_timer(vs, 30, "b", now=0.0)    # due at 30
    assert t.pop_due(vs, now=5.0) == []
    fired = t.pop_due(vs, now=15.0)
    assert [x.label for x in fired] == ["a"]
    assert [x.label for x in t.active(vs)] == ["b"]


def test_cancel_variants():
    vs = VoiceSession("s", None)
    assert t.cancel(vs) == ("none", None)
    t.add_timer(vs, 600, "pasta", now=0.0)
    status, only = t.cancel(vs)                 # single, no label
    assert status == "ok" and only.label == "pasta"
    assert t.active(vs) == []

    t.add_timer(vs, 600, "pasta", now=0.0)
    t.add_timer(vs, 60, "eggs", now=0.0)
    assert t.cancel(vs)[0] == "ambiguous"       # >1, no label
    status, tm = t.cancel(vs, "EGGS")           # case-insensitive label
    assert status == "ok" and tm.label == "eggs"
    assert t.cancel(vs, "nope")[0] == "none"    # unknown label


# -- provider backends ------------------------------------------------------

async def test_provider_commands_are_tier1():
    cmds = PROVIDER.commands(_ctx(VoiceSession("s", None)))
    ids = {c.id for c in cmds}
    assert ids == {"timer.set", "timer.list", "timer.cancel"}
    assert all(c.tier == 1 for c in cmds)
    assert await PROVIDER.detect(_ctx(None)) is True


async def test_set_via_seconds_then_list():
    vs = VoiceSession("s", None)
    ctx = _ctx(vs)
    r = await set_timer(ctx, seconds=600, label="pasta")
    assert r.success and "10 minutes" in r.output and "pasta" in r.output
    assert len(t.active(vs)) == 1
    rl = await list_timers(ctx)
    assert "left on pasta" in rl.output


async def test_set_via_duration_string():
    vs = VoiceSession("s", None)
    r = await set_timer(_ctx(vs), duration="5 minutes")
    assert r.success and "5 minutes" in r.output
    assert t.active(vs)[0].set_secs == 300


async def test_set_rejects_bad_durations():
    vs = VoiceSession("s", None)
    ctx = _ctx(vs)
    assert not (await set_timer(ctx)).success                       # nothing given
    assert not (await set_timer(ctx, seconds=0)).success            # zero
    assert not (await set_timer(ctx, seconds=100000000)).success    # > 24h
    assert t.active(vs) == []


async def test_set_max_count():
    vs = VoiceSession("s", None)
    ctx = _ctx(vs)
    for _ in range(t.MAX_TIMERS):
        assert (await set_timer(ctx, seconds=600)).success
    overflow = await set_timer(ctx, seconds=600)
    assert not overflow.success and "cancel one" in overflow.error


async def test_backends_require_voice_session():
    ctx = types.SimpleNamespace(voice_session=None)
    for fn in (lambda: set_timer(ctx, seconds=60), lambda: list_timers(ctx), lambda: cancel_timer(ctx)):
        r = await fn()
        assert not r.success and "voice mode" in r.error


async def test_list_empty():
    r = await list_timers(_ctx(VoiceSession("s", None)))
    assert "no timers" in r.output


async def test_cancel_backend_messages():
    vs = VoiceSession("s", None)
    ctx = _ctx(vs)
    assert "no timers to cancel" in (await cancel_timer(ctx)).output
    await set_timer(ctx, seconds=600, label="pasta")
    assert "Cancelled the pasta timer" in (await cancel_timer(ctx, label="pasta")).output
    await set_timer(ctx, seconds=600, label="pasta")
    await set_timer(ctx, seconds=60, label="eggs")
    amb = await cancel_timer(ctx)
    assert "Which should I cancel" in amb.output
    assert "don't have a timer called nope" in (await cancel_timer(ctx, label="nope")).output


# -- firing (the ticker's per-tick unit) ------------------------------------

async def test_fire_due_pops_and_returns(tmp_path, monkeypatch):
    # fire_due now enqueues a ring onto the disk-backed announce store — redirect XDG so it writes under
    # tmp, never the real ~/.local/share/gabagent/announce (a leak there floods the live voice side).
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    vs = VoiceSession("s", None)
    t.add_timer(vs, 10, "a", now=0.0)
    t.add_timer(vs, 30, "b", now=0.0)
    sessions = {"s": vs}
    ctx = types.SimpleNamespace(voice_debug_path=None)

    assert await t.fire_due(ctx, sessions, now=5.0) == []     # none due
    fired = await t.fire_due(ctx, sessions, now=20.0)
    assert [x.label for x in fired] == ["a"]
    assert [x.label for x in t.active(vs)] == ["b"]           # b still pending


async def test_fire_due_across_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))  # isolate the announce-store write
    v1, v2 = VoiceSession("s1", None), VoiceSession("s2", None)
    t.add_timer(v1, 10, "a", now=0.0)
    t.add_timer(v2, 10, "b", now=0.0)
    fired = await t.fire_due(types.SimpleNamespace(voice_debug_path=None), {"s1": v1, "s2": v2}, now=15.0)
    assert {x.label for x in fired} == {"a", "b"}


# -- the contract event (delivery held; constructor ready) ------------------

def test_timer_fired_event_shape():
    d = events.timer_fired("t1", "pasta", 600).to_dict()
    assert d == {"type": "timer_fired", "id": "t1", "label": "pasta", "set_secs": 600}
