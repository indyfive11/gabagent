"""P2: the Tier-1 reconciler (consolidate + prune, NO escalation). Covers the structured fact store,
the merge/preserve/prune/pin semantics, and the gated end-to-end consolidate pass."""
import types

import pytest

from gabagent.api.models import ChatMessage
from gabagent.config.models import GabAgentConfig
from gabagent.tmi.facts import Fact, FactStore, normalize, iso_week
from gabagent.tmi.reconciler import TmiReconciler


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path


# --- fact store + helpers -------------------------------------------------

def test_normalize_dedup_key():
    assert normalize("  - Likes JAZZ  ") == normalize("likes jazz") == "likes jazz"


def test_iso_week_buckets_two_weeks_apart_differ():
    t = 1_700_000_000.0
    assert iso_week(t) != iso_week(t + 14 * 86400)


def test_factstore_roundtrip_and_bad_line_tolerated(isolated, tmp_path):
    p = tmp_path / "facts.jsonl"
    store = FactStore(p)
    store.save([Fact(text="round table", sources=["den"], first_seen=1.0, last_seen=1.0,
                     hits=2, seen_weeks=["2023-W46"], pinned=True)])
    p.write_text(p.read_text() + "not json\n", encoding="utf-8")  # corrupt a trailing line
    loaded = store.load()
    assert len(loaded) == 1 and loaded[0].text == "round table" and loaded[0].pinned is True


def test_fact_observe_bumps_hits_week_source():
    f = Fact(text="x", sources=["den"], first_seen=1.0, last_seen=1.0, hits=1, seen_weeks=["2020-W01"])
    f.observe(1_700_000_000.0, "kitchen")  # a clearly different week than the seed
    assert f.hits == 2 and "kitchen" in f.sources and len(f.seen_weeks) == 2


# --- merge: preserve, accrue, prune, keep-pinned --------------------------

def test_merge_preserves_first_seen_prunes_dropped_keeps_pinned():
    t0 = 1_700_000_000.0
    existing = [
        Fact(text="likes jazz", sources=["den"], first_seen=t0, last_seen=t0, hits=1, seen_weeks=[iso_week(t0)]),
        Fact(text="stale note", sources=["den"], first_seen=t0, last_seen=t0, hits=1, seen_weeks=[iso_week(t0)]),
        Fact(text="PIN me", sources=["den"], first_seen=t0, last_seen=t0, hits=1, seen_weeks=[iso_week(t0)], pinned=True),
    ]
    t1 = t0 + 14 * 86400  # two weeks later
    merged = TmiReconciler("den")._merge(existing, ["- likes jazz", "- brand new"], t1)
    by = {f.key: f for f in merged}
    assert by["likes jazz"].first_seen == t0 and by["likes jazz"].hits == 2  # preserved + accrued
    assert len(by["likes jazz"].seen_weeks) == 2                              # distinct-week recorded
    assert by["brand new"].hits == 1                                         # net-new
    assert "stale note" not in by                                            # non-pinned, dropped => pruned
    assert "pin me" in by                                                    # pinned kept even though not re-emitted


# --- gated end-to-end reconcile ------------------------------------------

class _FakeClient:
    def __init__(self, out): self._out = out
    async def complete_simple(self, prompt, model=None, effort=None):
        return self._out


def _ctx(tmp_path, *, enabled, room_id, n_user_turns=4):
    turns = []
    for i in range(n_user_turns):
        turns.append(ChatMessage(role="user", content=f"line {i}"))
        turns.append(ChatMessage(role="assistant", content=f"ok {i}"))
    cfg = GabAgentConfig(api_key="t", tmi={"enabled": enabled})
    return types.SimpleNamespace(
        config=cfg, cwd=tmp_path, room_id=room_id,
        session=types.SimpleNamespace(messages=lambda: turns),
        local_floor=False, local_client=None, degraded_backends=set(), clients={},
    )


async def test_reconcile_disabled_is_noop(isolated):
    ctx = _ctx(isolated, enabled=False, room_id="den")
    assert await TmiReconciler("den").reconcile_from_ctx(ctx) is False


async def test_reconcile_skips_trivial_session(isolated):
    ctx = _ctx(isolated, enabled=True, room_id="den", n_user_turns=2)  # < _MIN_TURNS
    assert await TmiReconciler("den").reconcile_from_ctx(ctx) is False


async def test_reconcile_writes_room_facts_and_index(isolated, monkeypatch):
    from gabagent.persona.manager import PersonaManager
    from gabagent.config.paths import room_dir
    out = "<FACTS>\n- the user likes jazz in the evening\n- the coffee table is round\n</FACTS>\n<NOTE>added</NOTE>"
    monkeypatch.setattr(PersonaManager, "_reflection_client",
                        staticmethod(lambda ctx: (_FakeClient(out), None, None)))
    ctx = _ctx(isolated, enabled=True, room_id="living_room")
    wrote = await TmiReconciler("living_room").reconcile_from_ctx(ctx, now=1_700_000_000.0)
    assert wrote is True
    facts = FactStore(room_dir("living_room") / "facts.jsonl").load()
    assert {f.text for f in facts} == {"the user likes jazz in the evening", "the coffee table is round"}
    assert all(f.hits == 1 and f.sources == ["living_room"] for f in facts)
    index = (room_dir("living_room") / "memory.md").read_text()
    assert "likes jazz in the evening" in index
