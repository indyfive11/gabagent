"""Unit tests for the Stratum memory subsystem (docs/STRATUM.md)."""
from __future__ import annotations

import pathlib

import pytest

from gabagent.agent.context import AgentContext
from gabagent.api.models import ChatMessage, ToolCallSpec, ToolResult
from gabagent.config.models import GabAgentConfig
from gabagent.stratum import active
from gabagent.stratum import current_focus as CF
from gabagent.stratum import inject, observer
from gabagent.stratum.observed import Habit, ObservedStore, normalize


# --------------------------------------------------------------------------- config no-op
def test_stratum_config_default_is_off():
    c = GabAgentConfig()
    assert c.stratum.enabled is False
    assert c.stratum.observation_mode == "surface"
    assert c.stratum.compact_prep_ratio < 0.85  # must stay below the compaction ratio


# --------------------------------------------------------------------------- active() gate
def _ctx(cfg, **kw):
    kw.setdefault("cwd", pathlib.Path.cwd())
    return AgentContext(config=cfg, client=None, rate_limiter=None, session=None,
                        session_id="t", **kw)


def test_active_gate():
    off = GabAgentConfig()
    on = GabAgentConfig()
    on.stratum.enabled = True
    assert active(_ctx(off)) is False                       # disabled
    assert active(_ctx(on)) is True                          # enabled, top-level
    assert active(_ctx(on, is_subagent=True)) is False       # gated OFF in sub-agents


# --------------------------------------------------------------------------- observed store
def test_observed_accrete_reinforce_and_immutable_heading(tmp_path):
    s = ObservedStore(tmp_path / "obs.md")
    s.accrete("User tends to test before committing")
    s.accrete("  user TENDS to test before committing  ")  # normalized match → reinforce, not new
    hs = s.load()
    assert len(hs) == 1
    assert hs[0].hits == 2
    assert hs[0].heading == "User tends to test before committing"  # heading unchanged on reinforce


def test_observed_score_orders_by_hits_and_definitive():
    a = Habit(heading="a", hits=1, last_seen="2026-08-12")
    b = Habit(heading="b", hits=10, last_seen="2026-08-12")
    d = Habit(heading="d", hits=1, origin="definitive", last_seen="2026-08-12")
    import time
    now = time.time()
    assert b.score(now, 30) > a.score(now, 30)          # more hits ranks higher
    assert d.score(now, 30) > a.score(now, 30)          # definitive outranks an equal observed


def test_observed_conflict_penalizes_and_marks(tmp_path):
    s = ObservedStore(tmp_path / "obs.md")
    s.accrete("User tends to X")
    assert s.mark_conflict("user tends to x") is True
    assert s.load()[0].conflict_count == 1


def test_observed_render_block_is_subordinate_and_budgeted(tmp_path):
    s = ObservedStore(tmp_path / "obs.md")
    for i in range(50):
        s.accrete(f"User tends to do thing number {i}")
    block = s.render_block(top=35, budget=3000)
    assert "SUBORDINATE" in block
    assert len(block) <= 3000 + 200  # budget respected (+ header slack)
    # candidate/rejected states are excluded from the injected block
    s.accrete("a hidden candidate", state="candidate")
    assert "hidden candidate" not in s.render_block()


def test_observed_prune_protects_eligible(tmp_path):
    s = ObservedStore(tmp_path / "obs.md")
    for i in range(10):
        s.accrete(f"habit {i}")
    hs = s.load()
    hs[0].state = "eligible"          # protected
    s.save(hs)
    s.prune(soft_cap=5, hard_cap=75, halflife_days=30)
    kept = s.load()
    assert len(kept) == 5
    assert any(h.state == "eligible" for h in kept)  # the protected one survived


def test_observed_advance_gate(tmp_path):
    s = ObservedStore(tmp_path / "obs.md")
    s.accrete("frequent habit")
    h = s.load()[0]
    h.accreted = "2026-01-01"                    # old enough
    h.hits = 9
    h.seen_weeks = ["2026-W01", "2026-W02", "2026-W03"]
    s.save([h])
    newly = s.advance(adv_days=30, adv_hits=5, adv_weeks=3)
    assert len(newly) == 1
    assert s.load()[0].state == "eligible"


def test_observed_serialize_roundtrip(tmp_path):
    s = ObservedStore(tmp_path / "obs.md")
    s.accrete("User tends to Y", origin="definitive")
    reloaded = ObservedStore(tmp_path / "obs.md").load()
    assert reloaded[0].heading == "User tends to Y"
    assert reloaded[0].origin == "definitive"


def test_normalize():
    assert normalize("## User TENDS  to  X") == "user tends to x"
    assert normalize("- user tends to x") == "user tends to x"


# --------------------------------------------------------------------------- current focus
def test_current_focus_extract_and_upsert_replace():
    mem = "## Current Focus\n\nDoing: old\n\n## Notes\n\nkeep me\n"
    assert "Doing: old" in CF.extract_block(mem)
    out = CF.upsert_block(mem, "Doing: new")
    assert "Doing: new" in out and "Doing: old" not in out
    assert "## Notes" in out and "keep me" in out  # other sections preserved


def test_current_focus_insert_at_top_when_absent():
    out = CF.upsert_block("just some memory\n", "Doing: fresh")
    assert out.startswith("## Current Focus")
    assert "just some memory" in out


def test_current_focus_size_reminder_bands():
    c = GabAgentConfig().stratum
    small = "## Current Focus\n\ndoing\n"
    assert CF.size_reminder(small, c) == ""
    big = "## Current Focus\n" + "\n".join(f"- l{i}" for i in range(200))
    r = CF.size_reminder(big, c)
    assert r and "LINES" in r


# --------------------------------------------------------------------------- observer
def test_observer_captures_error_and_repeat(tmp_path):
    c = GabAgentConfig()
    c.stratum.enabled = True
    c.stratum.repeat_signal_threshold = 3
    ctx = _ctx(c)
    tc = ToolCallSpec(id="1", name="shell", arguments='{"cmd":"ls"}')
    err = ToolResult(output="", error="denied")
    ok = ToolResult(output="ok")
    observer.capture(ctx, [tc], [err])
    assert any(s["kind"] == "tool_error" and s["tool"] == "shell" for s in ctx.stratum_signals)
    # identical (tool,args) three times → exactly one repeat signal at the threshold
    for _ in range(3):
        observer.capture(ctx, [tc], [ok])
    repeats = [s for s in ctx.stratum_signals if s["kind"] == "repeat"]
    assert len(repeats) == 1
    assert "repeated identical calls" in observer.summary(ctx)


# --------------------------------------------------------------------------- inject
def test_inject_empty_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from gabagent.config.paths import observed_habits_file
    ctx = _ctx(GabAgentConfig())  # disabled
    assert inject.session_block(ctx) == ""
    assert not observed_habits_file().exists()  # zero new files while disabled


def test_inject_block_present_when_enabled_with_habits(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from gabagent.config.paths import observed_habits_file
    ObservedStore(observed_habits_file()).accrete("User tends to run tests first")
    c = GabAgentConfig()
    c.stratum.enabled = True
    block = inject.session_block(_ctx(c, cwd=tmp_path))
    assert "SUBORDINATE" in block and "run tests first" in block
    # sub-agent gets nothing
    assert inject.session_block(_ctx(c, cwd=tmp_path, is_subagent=True)) == ""


# --------------------------------------------------------------------------- compact-prep
class _FakeSession:
    def __init__(self, msgs):
        self._m = msgs

    def messages(self):
        return self._m


class _FakeClient:
    def __init__(self, out):
        self.out = out
        self.calls = 0

    async def complete_simple(self, messages, model=None, effort=None):
        self.calls += 1
        return self.out


def _prep_ctx(tmp_path, out, mode="surface"):
    c = GabAgentConfig()
    c.stratum.enabled = True
    c.stratum.observation_mode = mode
    msgs = []
    for i in range(4):
        msgs.append(ChatMessage(role="user", content=f"do task {i}"))
        msgs.append(ChatMessage(role="assistant", content=f"did task {i}"))
    return AgentContext(
        config=c, client=_FakeClient(out), rate_limiter=None,
        session=_FakeSession(msgs), session_id="t", cwd=tmp_path,
    )


@pytest.mark.asyncio
async def test_compact_prep_writes_window_and_surfaces_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from gabagent.config.paths import memory_file, observed_habits_file
    from gabagent.session.memory import MemoryManager
    from gabagent.stratum import compact_prep

    out = ("<CURRENT_FOCUS>Doing (since 2026-08-12): stratum build</CURRENT_FOCUS>\n"
           "<HABITS>User tends to run tests before committing</HABITS>\n"
           "<NOTE>the loop merges system msgs at assembly</NOTE>")
    ctx = _prep_ctx(tmp_path, out, mode="surface")
    await compact_prep.run(ctx)

    mem = MemoryManager(ctx.cwd).load()
    assert "## Current Focus" in mem and "stratum build" in mem
    assert "[stratum]" in mem  # the NOTE was appended
    habits = ObservedStore(observed_habits_file()).load()
    assert len(habits) == 1
    assert habits[0].state == "candidate"  # surface mode → candidate, not injected


@pytest.mark.asyncio
async def test_compact_prep_auto_mode_accretes(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from gabagent.config.paths import observed_habits_file
    from gabagent.stratum import compact_prep

    out = "<HABITS>User tends to prefer master branch</HABITS>"
    ctx = _prep_ctx(tmp_path, out, mode="auto")
    await compact_prep.run(ctx)
    habits = ObservedStore(observed_habits_file()).load()
    assert len(habits) == 1 and habits[0].state == "accreting"


@pytest.mark.asyncio
async def test_compact_prep_zero_tags_leaves_memory_unchanged(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from gabagent.config.paths import memory_file
    from gabagent.stratum import compact_prep

    ctx = _prep_ctx(tmp_path, "sorry, I cannot help with that", mode="surface")
    await compact_prep.run(ctx)
    assert not memory_file(ctx.cwd).exists() or memory_file(ctx.cwd).read_text() == ""


@pytest.mark.asyncio
async def test_compact_prep_noop_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from gabagent.config.paths import memory_file
    from gabagent.stratum import compact_prep

    ctx = _prep_ctx(tmp_path, "<CURRENT_FOCUS>x</CURRENT_FOCUS>", mode="surface")
    ctx.config.stratum.enabled = False
    await compact_prep.run(ctx)
    assert ctx.client.calls == 0                      # no LLM call
    assert not memory_file(ctx.cwd).exists()          # nothing written


# --------------------------------------------------------------------------- loop wiring (integration)
class _PathSession(_FakeSession):
    def __init__(self, msgs, path):
        super().__init__(msgs)
        self.path = path

    def replace_all(self, msgs):
        self._m = msgs


@pytest.mark.asyncio
async def test_compact_context_invokes_prep_before_summary(tmp_path, monkeypatch):
    """The marquee seam: _compact_context runs compact-prep, and does so BEFORE the summary call —
    verifying the actual loop wiring, not just the unit. (The enabled-vs-disabled gate lives inside
    compact_prep.run and is covered by test_compact_prep_noop_when_disabled.)"""
    from gabagent.agent import loop
    import gabagent.stratum.compact_prep as CP

    order = []

    async def fake_prep(ctx):
        order.append("prep")

    monkeypatch.setattr(CP, "run", fake_prep)

    c = GabAgentConfig()
    c.stratum.enabled = True
    p = tmp_path / "s.jsonl"
    p.write_text("{}\n")
    msgs = []
    for i in range(4):
        msgs.append(ChatMessage(role="user", content=f"u{i}"))
        msgs.append(ChatMessage(role="assistant", content=f"a{i}"))

    class _OrderClient(_FakeClient):
        async def complete_simple(self, messages, model=None, effort=None):
            order.append("summary")
            return await super().complete_simple(messages, model, effort)

    ctx = AgentContext(
        config=c, client=_OrderClient("# Conversation Summary\n\nok"),
        rate_limiter=None, session=_PathSession(msgs, p), session_id="t", cwd=tmp_path,
        system_prompt="SP",
    )
    await loop._compact_context(ctx)
    assert order == ["prep", "summary"]  # prep runs, and strictly before the summary
