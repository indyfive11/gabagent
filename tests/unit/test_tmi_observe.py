"""TMI receipts (observe.py) — the durable audit trail for the detached reconcile + escalate passes.

These passes run in the shutdown child with no live voice session, so without these receipts a
*rejected* escalation leaves no trace at all (the exact gap that made the P3 shakedown un-diagnosable
without hand-replaying the judge). The trail must record what was nominated, what the judge kept, and
what landed in Tier 0 — and never raise."""
import json
import time as _time
import types

import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.tmi.facts import Fact, FactStore, iso_week
from gabagent.tmi.escalator import TmiEscalator
from gabagent.tmi.reconciler import TmiReconciler
from gabagent.tmi.observe import tmi_log

T = 1_700_000_000.0


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path


def _events():
    from gabagent.config.paths import tmi_dir
    path = tmi_dir() / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _fact(text, **kw):
    base = dict(sources=["den"], first_seen=T, last_seen=T, hits=1, seen_weeks=[iso_week(T)])
    base.update(kw)
    return Fact(text=text, **base)


# --- the logger itself ----------------------------------------------------

def test_tmi_log_appends_ts_event_and_fields(isolated):
    tmi_log("escalate", room="den", wrote=1)
    tmi_log("reconcile", room="den", added=2)
    rows = _events()
    assert [r["event"] for r in rows] == ["escalate", "reconcile"]
    assert rows[0]["room"] == "den" and rows[0]["wrote"] == 1
    assert "T" in rows[0]["ts"]  # ISO-T timestamp


def test_tmi_log_never_raises_on_unserializable(isolated):
    # a value json can't encode must not blow up the shutdown path
    tmi_log("escalate", weird=object())
    # nothing written, but no exception
    assert _events() == []


def test_trim_bounds_the_trail(isolated, monkeypatch):
    import gabagent.tmi.observe as observe
    monkeypatch.setattr(observe, "_MAX_LINES", 5)
    for i in range(25):
        tmi_log("tick", i=i)
    rows = _events()
    assert len(rows) <= 10  # trimmed back toward _MAX_LINES once past 2x
    assert rows[-1]["i"] == 24  # newest survives


# --- escalate receipts ----------------------------------------------------

class _FakeClient:
    def __init__(self, out): self._out = out
    async def complete_simple(self, prompt, model=None, effort=None):
        return self._out


def _ctx(tmp_path, *, enabled=True, escalation=True, room_id="den"):
    cfg = GabAgentConfig(api_key="t", tmi={"enabled": enabled, "tier0_escalation_enabled": escalation})
    return types.SimpleNamespace(
        config=cfg, cwd=tmp_path, room_id=room_id,
        session=types.SimpleNamespace(messages=lambda: []),
        local_floor=False, local_client=None, degraded_backends=set(), clients={},
    )


async def test_escalate_receipt_records_judge_rejection(isolated, monkeypatch):
    """The shakedown scenario: one explicit nominee the judge declines. The receipt must show it
    as rejected with wrote=0 — the trace that was missing on disk."""
    from gabagent.persona.manager import PersonaManager
    from gabagent.config.paths import room_dir
    FactStore(room_dir("den") / "facts.jsonl").save([_fact("wants snappier responses", explicit=True)])
    monkeypatch.setattr(PersonaManager, "_reflection_client",
                        staticmethod(lambda ctx: (_FakeClient("<KEEP>\n</KEEP>"), None, None)))
    await TmiEscalator("den").escalate_from_ctx(_ctx(isolated), now=T)
    rec = [r for r in _events() if r["event"] == "escalate"][-1]
    assert rec["room"] == "den"
    assert rec["nominated"] == ["wants snappier responses"]
    assert rec["confirmed"] == []
    assert rec["rejected"] == ["wants snappier responses"]
    assert rec["wrote"] == 0


async def test_escalate_receipt_records_success(isolated, monkeypatch):
    from gabagent.persona.manager import PersonaManager
    from gabagent.config.paths import room_dir
    FactStore(room_dir("den") / "facts.jsonl").save([_fact("takes coffee black", explicit=True)])
    monkeypatch.setattr(PersonaManager, "_reflection_client",
                        staticmethod(lambda ctx: (_FakeClient("<KEEP>\n- takes coffee black\n</KEEP>"), None, None)))
    await TmiEscalator("den").escalate_from_ctx(_ctx(isolated), now=T)
    rec = [r for r in _events() if r["event"] == "escalate"][-1]
    assert rec["confirmed"] == ["takes coffee black"]
    assert rec["rejected"] == []
    assert rec["wrote"] == 1


async def test_escalate_receipt_records_nothing_nominated(isolated):
    from gabagent.config.paths import room_dir
    FactStore(room_dir("den") / "facts.jsonl").save([_fact("just an observation")])  # not explicit, not habit
    await TmiEscalator("den").escalate_from_ctx(_ctx(isolated), now=T)
    rec = [r for r in _events() if r["event"] == "escalate"][-1]
    assert rec["nominated"] == [] and rec["wrote"] == 0


# --- reconcile receipts ---------------------------------------------------

async def test_reconcile_receipt_records_deltas(isolated, monkeypatch):
    from gabagent.persona.manager import PersonaManager
    from gabagent.config.paths import room_dir
    FactStore(room_dir("den") / "facts.jsonl").save([_fact("old fact"), _fact("dropped fact")])
    cfg = GabAgentConfig(api_key="t", tmi={"enabled": True})
    turns = [types.SimpleNamespace(role="user", content="hi")] * 4
    ctx = types.SimpleNamespace(
        config=cfg, cwd=isolated, room_id="den",
        session=types.SimpleNamespace(messages=lambda: turns),
        local_floor=False, local_client=None, degraded_backends=set(), clients={})
    # keep "old fact", drop "dropped fact", add "new fact" (one of them explicit)
    monkeypatch.setattr(PersonaManager, "_reflection_client",
                        staticmethod(lambda c: (_FakeClient("<FACTS>\n- old fact\n- [explicit] new fact\n</FACTS>"), None, None)))
    await TmiReconciler("den").reconcile_from_ctx(ctx, now=T)
    rec = [r for r in _events() if r["event"] == "reconcile"][-1]
    assert rec["before"] == 2 and rec["after"] == 2
    assert rec["added"] == 1 and rec["pruned"] == 1 and rec["kept"] == 1
    assert rec["explicit"] == 1


async def test_reconcile_receipt_records_too_few_turns(isolated):
    cfg = GabAgentConfig(api_key="t", tmi={"enabled": True})
    ctx = types.SimpleNamespace(
        config=cfg, cwd=isolated, room_id="den",
        session=types.SimpleNamespace(messages=lambda: [types.SimpleNamespace(role="user", content="hi")]),
        local_floor=False, local_client=None, degraded_backends=set(), clients={})
    await TmiReconciler("den").reconcile_from_ctx(ctx, now=T)
    rec = [r for r in _events() if r["event"] == "reconcile"][-1]
    assert rec["skipped"] == "too_few_turns" and rec["user_turns"] == 1
