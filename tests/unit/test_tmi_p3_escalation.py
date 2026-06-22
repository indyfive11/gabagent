"""P3: Tier-0 escalation — weighting, the adaptive bands, the explicit producer, nomination gate,
banded cap/eviction/deadlock, and the gated end-to-end pass + recall blend."""
import time as _time
import types

import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.tmi.facts import Fact, FactStore, iso_week
from gabagent.tmi import weights
from gabagent.tmi.escalator import TmiEscalator
from gabagent.tmi.reconciler import TmiReconciler

T = 1_700_000_000.0


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    return tmp_path


def _fact(text, **kw):
    base = dict(sources=["den"], first_seen=T, last_seen=T, hits=1, seen_weeks=[iso_week(T)])
    base.update(kw)
    return Fact(text=text, **base)


# --- weights + bands ------------------------------------------------------

def test_decay_halves_at_one_halflife():
    assert weights.decay(T, T, 30) == 1.0
    assert weights.decay(T - 30 * 86400, T, 30) == pytest.approx(0.5, abs=1e-6)


def test_explicit_and_crossroom_raise_score():
    plain = _fact("x")
    assert weights.score(_fact("x", explicit=True), T, 30) > weights.score(plain, T, 30)
    assert weights.score(_fact("x", sources=["a", "b"]), T, 30) > weights.score(plain, T, 30)


def test_band_boundaries():
    assert weights.band_for(19).name == "relaxed"
    assert weights.band_for(20).name == "medium"
    assert weights.band_for(30).name == "firm"
    assert weights.band_for(40).name == "strict"
    assert weights.band_for(50).name == "strict"
    # admit bar rises and prune floor stays strictly below it (hysteresis margin)
    b = weights.band_for(35)
    assert b.prune_floor < b.admit_bar


# --- explicit producer (reconciler merge) ---------------------------------

def test_merge_parses_explicit_marker_and_is_sticky():
    r = TmiReconciler("den")
    merged = r._merge([], ["- [explicit] always dim lights at night", "- the rug is blue"], T)
    by = {f.key: f for f in merged}
    assert by["always dim lights at night"].explicit is True
    assert by["the rug is blue"].explicit is False
    # sticky: re-emitted WITHOUT the marker stays explicit
    again = r._merge(merged, ["- always dim lights at night"], T)
    assert again[0].explicit is True


def test_merge_keeps_protected_on_omission():
    r = TmiReconciler("den")
    existing = [_fact("always X", explicit=True), _fact("transient Y")]
    merged = r._merge(existing, ["- something new"], T)  # neither existing re-emitted
    keys = {f.key for f in merged}
    assert "always x" in keys           # explicit kept
    assert "transient y" not in keys    # non-protected pruned by omission


# --- nomination gate ------------------------------------------------------

def _tmi(**kw):
    base = dict(habit_count=3, habit_window_days=14, tier0_hard_cap=50, tier0_exclude_rooms=[])
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_habit_requires_count_weeks_and_span():
    esc = TmiEscalator("den")
    tmi = _tmi()
    spike = _fact("spike", hits=5, seen_weeks=[iso_week(T)], first_seen=T, last_seen=T)  # one week
    assert esc._is_habit(spike, tmi, T) is False
    habit = _fact("habit", hits=3, first_seen=T - 20 * 86400, last_seen=T,
                  seen_weeks=[iso_week(T - 20 * 86400), iso_week(T)])
    assert esc._is_habit(habit, tmi, T) is True


def test_nominate_takes_explicit_or_habit_and_honors_privacy():
    esc = TmiEscalator("den")
    facts = [_fact("explicit one", explicit=True), _fact("nothing special")]
    assert {f.text for f in esc._nominate(facts, _tmi(), T)} == {"explicit one"}
    # privacy handled in escalate_from_ctx, but exclude empties the room contribution there
    esc2 = TmiEscalator("bedroom")
    assert esc2._nominate(facts, _tmi(), T)  # nomination itself is room-agnostic


# --- banded cap: admit, evict, protect, deadlock --------------------------

def test_apply_band_protects_pinned_from_prune_and_evict():
    esc = TmiEscalator("den")
    tmi = _tmi(tier0_hard_cap=2)
    old = _fact("weak pinned", pinned=True, last_seen=T - 999 * 86400)  # ancient, near-zero score
    tier0 = [old]
    strong = _fact("strong new", hits=10, sources=["a", "b", "c"])
    final, deadlock = esc._apply_band(tier0, [strong], tmi, T)
    keys = {f.key for f in final}
    assert "weak pinned" in keys and "strong new" in keys  # pinned survived, new admitted
    assert deadlock is False


def test_apply_band_deadlock_when_all_protected_at_cap():
    esc = TmiEscalator("den")
    tmi = _tmi(tier0_hard_cap=2)
    tier0 = [_fact("pin a", pinned=True), _fact("pin b", explicit=True)]
    strong = _fact("strong", hits=20, sources=["a", "b", "c", "d"])
    final, deadlock = esc._apply_band(tier0, [strong], tmi, T)
    assert deadlock is True
    assert "strong" not in {f.key for f in final}  # couldn't make room


def test_apply_band_evicts_weakest_for_a_stronger_candidate():
    esc = TmiEscalator("den")
    tmi = _tmi(tier0_hard_cap=1)
    weak = _fact("weak", hits=1, last_seen=T - 60 * 86400)
    strong = _fact("strong", hits=10, sources=["a", "b", "c"], explicit=True)
    final, _ = esc._apply_band([weak], [strong], tmi, T)
    keys = {f.key for f in final}
    assert "strong" in keys and "weak" not in keys


# --- end-to-end escalation + recall blend ---------------------------------

class _FakeClient:
    def __init__(self, out): self._out = out
    async def complete_simple(self, prompt, model=None, effort=None):
        return self._out


def _ctx(tmp_path, *, enabled, escalation, room_id="den"):
    cfg = GabAgentConfig(api_key="t", tmi={"enabled": enabled, "tier0_escalation_enabled": escalation})
    return types.SimpleNamespace(
        config=cfg, cwd=tmp_path, room_id=room_id,
        session=types.SimpleNamespace(messages=lambda: []),
        local_floor=False, local_client=None, degraded_backends=set(), clients={},
    )


async def test_escalate_disabled_is_noop(isolated):
    assert await TmiEscalator("den").escalate_from_ctx(_ctx(isolated, enabled=True, escalation=False)) == 0


async def test_escalate_writes_habit_fact_to_tier0(isolated, monkeypatch):
    from gabagent.persona.manager import PersonaManager
    from gabagent.config.paths import room_dir, tier0_dir
    # a qualifying habit fact in the room's Tier 1
    habit = _fact("the user takes coffee black", hits=3, first_seen=T - 20 * 86400, last_seen=T,
                  seen_weeks=[iso_week(T - 20 * 86400), iso_week(T)])
    FactStore(room_dir("den") / "facts.jsonl").save([habit])
    monkeypatch.setattr(PersonaManager, "_reflection_client",
                        staticmethod(lambda ctx: (_FakeClient("<KEEP>\n- the user takes coffee black\n</KEEP>"), None, None)))
    n = await TmiEscalator("den").escalate_from_ctx(_ctx(isolated, enabled=True, escalation=True), now=T)
    assert n == 1
    tier0 = FactStore(tier0_dir() / "facts.jsonl").load()
    assert {f.text for f in tier0} == {"the user takes coffee black"}


async def test_judge_can_reject(isolated, monkeypatch):
    from gabagent.persona.manager import PersonaManager
    from gabagent.config.paths import room_dir, tier0_dir
    FactStore(room_dir("den") / "facts.jsonl").save([_fact("room-specific clutter", explicit=True)])
    monkeypatch.setattr(PersonaManager, "_reflection_client",
                        staticmethod(lambda ctx: (_FakeClient("<KEEP>\n</KEEP>"), None, None)))  # rejects all
    n = await TmiEscalator("den").escalate_from_ctx(_ctx(isolated, enabled=True, escalation=True), now=T)
    assert n == 0
    assert FactStore(tier0_dir() / "facts.jsonl").load() == []


async def test_privacy_excluded_room_never_escalates(isolated, monkeypatch):
    from gabagent.config.paths import room_dir
    FactStore(room_dir("bedroom") / "facts.jsonl").save([_fact("secret", explicit=True)])
    ctx = _ctx(isolated, enabled=True, escalation=True, room_id="bedroom")
    ctx.config.tmi.tier0_exclude_rooms = ["bedroom"]
    assert await TmiEscalator("bedroom").escalate_from_ctx(ctx, now=T) == 0


def test_recall_blends_tier0_facts_when_enabled(isolated):
    from gabagent.tmi.manager import TmiManager
    from gabagent.config.paths import tier0_dir
    FactStore(tier0_dir() / "facts.jsonl").save([_fact("the user is a night owl", explicit=True)])
    ctx = _ctx(isolated, enabled=True, escalation=True)
    brief = TmiManager(ctx).tier0_brief()
    assert "night owl" in brief and "Shared across every room" in brief


def test_recall_persona_only_when_escalation_off(isolated):
    from gabagent.tmi.manager import TmiManager
    from gabagent.config.paths import tier0_dir
    FactStore(tier0_dir() / "facts.jsonl").save([_fact("should not appear", explicit=True)])
    ctx = _ctx(isolated, enabled=True, escalation=False)
    assert "should not appear" not in TmiManager(ctx).tier0_brief()
