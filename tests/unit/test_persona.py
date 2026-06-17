"""Self-learning persona layer: global store, bounded injection, archive pruning, reflection loop."""
import types

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


# -- parser -------------------------------------------------------------------

def test_between_extracts_tagged_sections():
    assert _between("<INDEX>hello</INDEX>", "INDEX") == "hello"
    assert _between("<JOURNAL>note", "JOURNAL") == "note"  # tolerant of missing close
    assert _between("nothing here", "INDEX") == ""
