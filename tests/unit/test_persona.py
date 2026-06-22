"""Self-learning persona layer: global store, bounded injection, archive pruning, reflection loop."""
import json
import types
from pathlib import Path

import pytest

from gabagent.api.models import ChatMessage
from gabagent.agent.system_prompt import build_system_prompt
from gabagent.persona.manager import PersonaManager, _between, _INDEX_CAP, _MIN_TURNS


@pytest.fixture
def persona(tmp_path, monkeypatch):
    # data_dir() honors XDG_DATA_HOME → redirect the whole persona store into tmp_path.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    return PersonaManager()


# -- store --------------------------------------------------------------------

def test_seed_materializes_and_brief_includes_it(persona):
    brief = persona.brief()
    assert "Aria" in persona.seed()
    assert persona.seed() in brief  # the floor is always present


def test_index_roundtrip_and_brief(persona):
    persona.rewrite_index("- teases the user about typos\n- deadpan about errors")
    assert "teases the user about typos" in persona.index()
    assert "Learned with this user" in persona.brief()
    assert "deadpan about errors" in persona.brief()


def test_index_bounded_on_write(persona):
    persona.rewrite_index("x" * (_INDEX_CAP + 500))
    assert len(persona.index()) <= _INDEX_CAP + 2  # cap + the truncation ellipsis


def test_reset_keeps_seed_clears_learned(persona):
    persona.rewrite_index("- some trait")
    persona.append_journal("saw the trait")
    persona.reset()
    assert persona.index() == ""
    assert persona.journal() == ""
    assert "Aria" in persona.seed()  # rail #5: the seed is the floor


def test_journal_archives_when_large(persona, tmp_path):
    for i in range(260):
        persona.append_journal(f"observation {i}")
    journal_path = tmp_path / "gabagent" / "persona" / "journal.md"
    archive_path = tmp_path / "gabagent" / "persona" / "journal-archive.md"
    assert archive_path.exists()  # oldest lines moved out
    # Bounded: trims to 50 whenever it crosses 200, so it never runs away.
    assert len(journal_path.read_text().splitlines()) < 200


# -- system-prompt injection --------------------------------------------------

def test_persona_injected_into_system_prompt(tmp_path):
    sp = build_system_prompt(cwd=tmp_path, persona="You are dry and warm.")
    assert "# Persona" in sp
    assert "You are dry and warm." in sp


def test_persona_absent_when_none(tmp_path):
    sp = build_system_prompt(cwd=tmp_path)
    assert "# Persona" not in sp


# -- voice-character hint (HYBRID seam for VAC) -------------------------------

def test_voice_hint_written_and_read(persona):
    assert persona.write_voice_hint("dry", 0.7) is True
    h = persona.voice_hint()
    assert h["character"] == "dry" and h["confidence"] == 0.7 and "ts" in h


def test_voice_hint_transition_only(persona):
    assert persona.write_voice_hint("dry", 0.5) is True
    assert persona.write_voice_hint("dry", 0.9) is False  # unchanged character → no churn
    assert persona.voice_hint()["confidence"] == 0.5  # ts/confidence not bumped
    assert persona.write_voice_hint("warm", 0.6) is True  # real change → written


def test_voice_hint_rejects_invalid_character(persona):
    assert persona.write_voice_hint("sultry", 0.9) is False
    assert persona.voice_hint() == {}


def test_voice_hint_atomic_no_temp_leftover(persona, tmp_path):
    persona.write_voice_hint("calm", 0.4)
    pdir = tmp_path / "gabagent" / "persona"
    assert [p.name for p in pdir.iterdir() if p.name.startswith(".voice_hint.")] == []


def test_reset_clears_voice_hint(persona):
    persona.write_voice_hint("energetic", 0.8)
    persona.reset()
    assert persona.voice_hint() == {}  # fail-safe back to the user's manual pick


def test_confidence_clamped(persona):
    persona.write_voice_hint("warm", 5.0)
    assert persona.voice_hint()["confidence"] == 1.0


# -- reflection ---------------------------------------------------------------

class _FakeClient:
    def __init__(self, out):
        self.out = out
        self.calls = []

    async def complete_simple(self, messages, model=None, effort=None):
        self.calls.append(messages)
        return self.out


def _ctx_with_turns(persona, n_user, monkeypatch, fake):
    msgs = []
    for i in range(n_user):
        msgs.append(ChatMessage(role="user", content=f"u{i} I like it dry"))
        msgs.append(ChatMessage(role="assistant", content=f"a{i}"))
    ctx = types.SimpleNamespace(
        session=types.SimpleNamespace(messages=lambda: list(msgs)),
        config=types.SimpleNamespace(),
        local_floor=False,
        local_client=None,
        degraded_backends=set(),
    )
    # Isolate the reflection logic from routing — force the top-rung resolver to our fake.
    monkeypatch.setattr(PersonaManager, "_reflection_client", staticmethod(lambda ctx: (fake, None, None)))
    return ctx


@pytest.mark.asyncio
async def test_reflection_rewrites_index_and_journal(persona, monkeypatch):
    fake = _FakeClient(
        "<INDEX>\n- dry humor (the user said so)\n</INDEX>\n"
        "<JOURNAL>\nthe user asked for dry\n</JOURNAL>\n<VOICE>\ndry|0.8\n</VOICE>"
    )
    ctx = _ctx_with_turns(persona, _MIN_TURNS, monkeypatch, fake)
    await persona.reflect_from_ctx(ctx)
    assert "dry humor" in persona.index()
    assert "the user asked for dry" in persona.journal()
    assert persona.voice_hint()["character"] == "dry"  # VOICE section flowed to the hint
    assert persona.voice_hint()["confidence"] == 0.8
    assert fake.calls  # the model was actually consulted


@pytest.mark.asyncio
async def test_reflection_skips_trivial_session(persona, monkeypatch):
    persona.rewrite_index("- untouched")
    fake = _FakeClient("<INDEX>\n- should not be written\n</INDEX>\n<JOURNAL>\nx\n</JOURNAL>")
    ctx = _ctx_with_turns(persona, _MIN_TURNS - 1, monkeypatch, fake)
    await persona.reflect_from_ctx(ctx)
    assert persona.index() == "- untouched"  # guarded out — nothing to learn
    assert not fake.calls


@pytest.mark.asyncio
async def test_reflection_no_change_skips_journal(persona, monkeypatch):
    fake = _FakeClient("<INDEX>\n- dry\n</INDEX>\n<JOURNAL>\nno change\n</JOURNAL>")
    ctx = _ctx_with_turns(persona, _MIN_TURNS, monkeypatch, fake)
    await persona.reflect_from_ctx(ctx)
    assert persona.journal() == ""  # 'no change' is not recorded


@pytest.mark.asyncio
async def test_reflection_swallows_client_error(persona, monkeypatch):
    class _Boom:
        async def complete_simple(self, *a, **k):
            raise RuntimeError("network down")
    ctx = _ctx_with_turns(persona, _MIN_TURNS, monkeypatch, _Boom())
    await persona.reflect_from_ctx(ctx)  # must not raise


# -- detached reflection (survives the brain's SIGTERM/SIGKILL) ---------------

def _cli_ctx(enabled=True, n_user=4):
    msgs = []
    for i in range(n_user):
        msgs.append(ChatMessage(role="user", content=f"u{i} keep it dry"))
        msgs.append(ChatMessage(role="assistant", content=f"a{i}"))
    return types.SimpleNamespace(
        config=types.SimpleNamespace(persona_enabled=enabled, persona_reflect_on_shutdown=enabled),
        session=types.SimpleNamespace(messages=lambda: list(msgs)),
    )


@pytest.mark.asyncio
async def test_persona_reflect_spawns_in_systemd_scope(monkeypatch):
    # Shutdown reflection must NOT run in-process (it loses the race to the front-end's 5s SIGKILL). On a
    # systemd host it spawns into its OWN transient scope (separate cgroup) so it survives the
    # voice-agent.service cgroup being cycled on every brain teardown — process-group escape alone is reaped.
    import shutil
    import subprocess
    from gabagent import cli
    captured = {}

    class _FakePopen:
        def __init__(self, argv, **kw):
            captured["argv"], captured["kw"] = argv, kw

    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    await cli._persona_reflect(_cli_ctx(enabled=True, n_user=4))

    argv = captured["argv"]
    assert argv[0] == "/usr/bin/systemd-run"
    assert "--user" in argv and "--scope" in argv  # own transient cgroup, escapes the unit's cgroup
    assert "gabagent.persona.reflect_detached" in argv
    assert captured["kw"]["start_new_session"] is True
    handoff = Path(argv[-1])
    assert handoff.exists()
    payload = json.loads(handoff.read_text())
    assert len(payload["turns"]) == 8 and payload["turns"][0]["role"] == "user"
    handoff.unlink()


@pytest.mark.asyncio
async def test_persona_reflect_falls_back_without_systemd_run(monkeypatch):
    # No systemd-run (non-systemd host / TUI dev box) → fall back to a plain detached child (bare argv,
    # process-group escape only). Reflection still runs where there's no cgroup to dodge.
    import shutil
    import subprocess
    from gabagent import cli
    captured = {}

    class _FakePopen:
        def __init__(self, argv, **kw):
            captured["argv"], captured["kw"] = argv, kw

    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    await cli._persona_reflect(_cli_ctx(enabled=True, n_user=4))

    argv = captured["argv"]
    assert "systemd-run" not in argv[0]
    assert argv[1:3] == ["-m", "gabagent.persona.reflect_detached"]  # bare child, no wrapper
    assert captured["kw"]["start_new_session"] is True
    handoff = Path(argv[-1])
    assert handoff.exists()
    payload = json.loads(handoff.read_text())
    assert len(payload["turns"]) == 8
    handoff.unlink()


@pytest.mark.asyncio
async def test_persona_reflect_skips_trivial_session(monkeypatch):
    import subprocess
    from gabagent import cli
    called = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: called.append(1))
    await cli._persona_reflect(_cli_ctx(enabled=True, n_user=2))  # < _MIN_TURNS user turns
    assert not called  # not worth spawning a child


@pytest.mark.asyncio
async def test_persona_reflect_skips_when_disabled(monkeypatch):
    import subprocess
    from gabagent import cli
    called = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: called.append(1))
    await cli._persona_reflect(_cli_ctx(enabled=False, n_user=8))
    assert not called


def test_detached_load_turns_roundtrip(tmp_path):
    from gabagent.persona import reflect_detached
    h = tmp_path / "h.json"
    h.write_text(json.dumps({"turns": [{"role": "user", "content": "hi"},
                                       {"role": "assistant", "content": "yo"}]}))
    turns = reflect_detached._load_turns(h)
    assert [t.role for t in turns] == ["user", "assistant"] and turns[0].content == "hi"


@pytest.mark.asyncio
async def test_detached_run_reflects_into_store(persona, monkeypatch, tmp_path):
    # The child rebuilds a ctx from the handoff and writes the learned INDEX into the (redirected) store.
    from gabagent.persona import reflect_detached
    fake = _FakeClient("<INDEX>\n- dry, the user asked\n</INDEX>\n<JOURNAL>\nasked for dry\n</JOURNAL>")

    def _fake_build_ctx(turns, room_id=None):
        monkeypatch.setattr(PersonaManager, "_reflection_client",
                            staticmethod(lambda ctx: (fake, None, None)))
        return types.SimpleNamespace(
            session=types.SimpleNamespace(messages=lambda: list(turns)),
            config=types.SimpleNamespace(persona_enabled=True, persona_reflect_on_shutdown=True,
                                         tmi=types.SimpleNamespace(enabled=False)),
            local_floor=False, local_client=None, degraded_backends=set(), room_id=room_id)

    monkeypatch.setattr(reflect_detached, "_build_ctx", _fake_build_ctx)
    h = tmp_path / "h.json"
    h.write_text(json.dumps({"turns": [{"role": "user", "content": "keep it dry"}] * 4 +
                                       [{"role": "assistant", "content": "ok"}]}))
    await reflect_detached._run(h)
    assert "dry, the user asked" in persona.index()


def test_detached_main_deletes_handoff(monkeypatch, tmp_path):
    # main() always removes the handoff file, even on a no-op run — no /tmp litter.
    from gabagent.persona import reflect_detached

    async def _noop(_h):
        return None

    monkeypatch.setattr(reflect_detached, "_run", _noop)
    h = tmp_path / "h.json"
    h.write_text("{}")
    assert reflect_detached.main([str(h)]) == 0
    assert not h.exists()


# -- parser -------------------------------------------------------------------

def test_between_extracts_tagged_sections():
    assert _between("<INDEX>hello</INDEX>", "INDEX") == "hello"
    assert _between("<JOURNAL>note", "JOURNAL") == "note"  # tolerant of missing close
    assert _between("nothing here", "INDEX") == ""
