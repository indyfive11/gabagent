"""Latency-gated progress-ack (#7) — Aria fills a slow-turn silence with one short filler so it doesn't
feel like a dead stick, while fast turns stay terse."""
import asyncio
import types

import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.voice import turn as T
from gabagent.voice import events

from tests.unit.test_voice_turn import make_ctx, run_turn, FakeClient


def _ctx(ms=30, phrase="One moment."):
    cfg = GabAgentConfig(api_key="t", voice_progress_ack_ms=ms, voice_progress_ack_phrase=phrase)
    return types.SimpleNamespace(config=cfg)


async def _collect_emit():
    out = []
    async def emit(ev):
        out.append(ev)
    return out, emit


# --- watchdog unit behavior -------------------------------------------------

async def test_fires_when_turn_stays_silent():
    out, emit = await _collect_emit()
    await T._progress_ack_watchdog(_ctx(ms=20), emit, set(), {"acked": False})
    assert any(getattr(e, "type", None) == "status" and "moment" in e.text.lower() for e in out)


async def test_silent_when_a_token_already_spoke():
    out, emit = await _collect_emit()
    await T._progress_ack_watchdog(_ctx(ms=20), emit, {"token"}, {"acked": False})
    assert out == []          # speech already happened → no filler


async def test_silent_when_domain_phrase_already_claimed_the_filler():
    out, emit = await _collect_emit()
    await T._progress_ack_watchdog(_ctx(ms=20), emit, set(), {"acked": True})
    assert out == []


async def test_disabled_when_ms_is_zero():
    out, emit = await _collect_emit()
    await T._progress_ack_watchdog(_ctx(ms=0), emit, set(), {"acked": False})
    assert out == []


async def test_cancellation_is_clean():
    out, emit = await _collect_emit()
    task = asyncio.create_task(T._progress_ack_watchdog(_ctx(ms=5000), emit, set(), {"acked": False}))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert out == []          # cancelled before firing → nothing emitted


def test_phrase_is_config_overridable():
    assert T._progress_phrase(_ctx(phrase="Hang on.")) == "Hang on."
    assert T._progress_phrase(_ctx(phrase="")) == "One moment."   # falls back


# --- integration: a slow turn gets the filler, a fast one doesn't -----------

class SlowClient(FakeClient):
    """Yields its chunks only after `delay`s — simulates a slow/cold model think."""
    def __init__(self, responses, delay):
        super().__init__(responses)
        self._delay = delay

    async def stream_complete(self, messages, tools=None, model=None, retry_model=None, **kw):
        await asyncio.sleep(self._delay)
        for c in self.responses.pop(0):
            yield c


async def test_slow_turn_emits_progress_ack(tmp_path):
    ctx = make_ctx(tmp_path, [["Sure."]], voice_progress_ack_ms=40)
    ctx.client = SlowClient([["Sure."]], delay=0.25)
    evs = await run_turn(ctx, "tell me something")
    assert any(e.type == "status" and "moment" in e.text.lower() for e in evs)


async def test_fast_turn_has_no_progress_ack(tmp_path):
    ctx = make_ctx(tmp_path, [["Sure."]], voice_progress_ack_ms=2000)
    evs = await run_turn(ctx, "tell me something")
    assert not any(e.type == "status" and "moment" in e.text.lower() for e in evs)
