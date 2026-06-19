"""Aria eye state-file contract: atomic write, JSON shape, stale→off failsafe."""
import json
import time

import pytest

from gabagent import aria_state


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return tmp_path / "aria" / "state"


def test_set_and_read_roundtrip(tmp_state):
    aria_state.set_state("thinking", 0.0)
    assert tmp_state.exists()
    d = json.loads(tmp_state.read_text())
    assert d["state"] == "thinking" and "ts" in d and d["level"] == 0.0
    assert aria_state.read_state()["state"] == "thinking"


def test_level_is_recorded(tmp_state):
    aria_state.set_state("speaking", 0.73)
    assert json.loads(tmp_state.read_text())["level"] == 0.73


def test_invalid_state_falls_back_to_idle(tmp_state):
    aria_state.set_state("bogus")
    assert aria_state.read_state()["state"] == "idle"


def test_missing_file_reads_off(tmp_state):
    assert aria_state.read_state()["state"] == "off"


def test_stale_reads_off(tmp_state):
    aria_state.set_state("speaking")
    # Read far in the future → past STALE_SECS → off.
    future = time.time() + aria_state.STALE_SECS + 10
    assert aria_state.read_state(now=future)["state"] == "off"


def test_fresh_reads_live_state(tmp_state):
    aria_state.set_state("listening")
    assert aria_state.read_state(now=time.time())["state"] == "listening"


def test_all_states_accepted(tmp_state):
    for s in aria_state.STATES:
        aria_state.set_state(s)
        assert aria_state.read_state()["state"] == s


def test_sleeping_is_a_distinct_live_state(tmp_state):
    # "sleeping" (intentionally asleep) must be a valid, distinct state — not coerced to idle, and
    # not the same as "off" (which doubles as dead/stale). It still obeys the stale failsafe.
    assert "sleeping" in aria_state.STATES
    aria_state.set_state("sleeping")
    assert aria_state.read_state(now=time.time())["state"] == "sleeping"
    stale = time.time() + aria_state.STALE_SECS + 10
    assert aria_state.read_state(now=stale)["state"] == "off"  # decays to off without a heartbeat


def test_write_is_atomic_no_partial(tmp_state):
    # After a write, the only file is the final state file — no leftover temp files.
    aria_state.set_state("idle")
    leftovers = [p.name for p in tmp_state.parent.iterdir() if p.name.startswith(".state.")]
    assert leftovers == []
