"""Aria eye heartbeat: a long TUI turn must keep the eye lit. `agent/loop.py` stamps `thinking`
once at turn start; without a heartbeat the state file goes stale (aria_state.STALE_SECS=5s) and the
reader blanks the eye to `off` mid-think. _eye_heartbeat re-stamps `thinking` on an interval so `ts`
keeps advancing. (Bug reported by the voice side 2026-06-17 — TUI run_loop path only.)"""
import asyncio
import json
import time
import types

import pytest

from gabagent import aria_state
from gabagent.agent.loop import _eye, _eye_heartbeat


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return tmp_path / "aria" / "state"


def _ctx(aria_eye: bool):
    return types.SimpleNamespace(config=types.SimpleNamespace(aria_eye=aria_eye))


async def test_heartbeat_restamps_thinking_and_keeps_fresh(tmp_state):
    # A turn longer than the stale window: the heartbeat must refresh `ts` so the eye reads live.
    hb = asyncio.create_task(_eye_heartbeat(_ctx(True), interval=0.01))
    await asyncio.sleep(0.05)  # several intervals
    hb.cancel()
    await hb
    assert tmp_state.exists()
    d = json.loads(tmp_state.read_text())
    assert d["state"] == "thinking"
    # ts is current → a reader sees `thinking`, not the stale-failsafe `off`.
    assert aria_state.read_state(now=time.time())["state"] == "thinking"


async def test_heartbeat_noop_when_eye_disabled(tmp_state):
    hb = asyncio.create_task(_eye_heartbeat(_ctx(False), interval=0.01))
    await asyncio.sleep(0.05)
    hb.cancel()
    await hb
    assert not tmp_state.exists()  # _eye no-ops → nothing written


async def test_error_state_survives_only_if_heartbeat_stopped_first(tmp_state):
    """loop.py's error path (the `except` at ~423) must stop the eye heartbeat BEFORE stamping
    `error`, or the 2s re-stamp flips it straight back to `thinking`. This mimics that ordering and
    proves the error state persists across would-be heartbeat ticks once the heartbeat is cancelled."""
    ctx = _ctx(True)
    hb = asyncio.create_task(_eye_heartbeat(ctx, interval=0.01))
    await asyncio.sleep(0.03)  # heartbeat establishes `thinking`
    assert aria_state.read_state(now=time.time())["state"] == "thinking"
    # error-path sequence: _stop_eye_hb() then _eye(ctx, "error")
    hb.cancel()
    await hb
    _eye(ctx, "error")
    await asyncio.sleep(0.03)  # several heartbeat intervals would have elapsed
    assert aria_state.read_state(now=time.time())["state"] == "error"  # not clobbered back to thinking


async def test_error_state_is_a_noop_when_eye_disabled(tmp_state):
    # aria_eye off (the voice-mode brain default) → the error stamp writes nothing; VAC owns the eye there.
    _eye(_ctx(False), "error")
    assert not tmp_state.exists()


async def test_heartbeat_cancels_cleanly(tmp_state):
    # Cancellation is swallowed (no CancelledError escapes) so the finally-cancel at turn end is quiet.
    hb = asyncio.create_task(_eye_heartbeat(_ctx(True), interval=0.01))
    await asyncio.sleep(0.02)
    hb.cancel()
    await hb  # must not raise
    assert hb.cancelled() is False and hb.exception() is None
