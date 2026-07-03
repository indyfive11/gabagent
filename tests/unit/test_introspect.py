"""Tests for the self-knowledge introspection layer (gabagent.introspect).

Covers the adversarial-review + VAC-consensus requirements:
  - C1-catcher: introspective utterances must REACH the model (detect_meta_command -> None), not be
    swallowed by the canned _Q_* readouts.
  - is_introspective true/false table incl. episodic-why exclusion and command-collision veto.
  - disclosure: the packaged doc names no internal model/vendor/host/port identifiers.
  - override precedence + size cap.
  - introspect_brief gating.
"""
from __future__ import annotations

import types

import pytest

from gabagent.introspect.detect import is_introspective
from gabagent.introspect.knowledge import about_text, _OVERRIDE_CAP
from gabagent.introspect import introspect_brief
from gabagent.voice.commands import detect_meta_command


# --- C1: introspective questions must reach the model (no canned-query swallow) ----------------------

INTROSPECTIVE = [
    "how do you decide which model to use",
    "how do you choose which brain to run",
    "what are your limits",
    "what can't you do",
    "what happens if the internet goes down",
    "do you need the internet",
    "how do you hear me",
    "how do you know it's me",
    "do you remember things",
    "how were you made",
    "tell me about yourself",
    "why do you keep your answers so short",
]


@pytest.mark.parametrize("utt", INTROSPECTIVE)
def test_introspective_reaches_model(utt):
    # The whole point of the C1 fix: these fall THROUGH detect_meta_command to the model.
    assert detect_meta_command(utt) is None, f"{utt!r} was swallowed by a meta/query handler"


@pytest.mark.parametrize("utt", INTROSPECTIVE)
def test_is_introspective_true(utt):
    assert is_introspective(utt) is True


# --- exclusions: episodic-why, command collisions, unrelated ----------------------------------------

EPISODIC_WHY = ["why did you do that", "why did you just stop", "why did you say that"]
COMMANDS = ["how do you play jazz", "how do you turn on the lights", "how do you set a timer for ten minutes"]
UNRELATED = ["how do you make a margarita", "what time is it", "play some music"]


@pytest.mark.parametrize("utt", EPISODIC_WHY + COMMANDS + UNRELATED)
def test_is_introspective_false(utt):
    assert is_introspective(utt) is False


@pytest.mark.parametrize("utt", EPISODIC_WHY)
def test_episodic_why_not_introspective(utt):
    # A static doc can't explain a specific past action — must NOT fire (VAC blocker-class finding).
    assert is_introspective(utt) is False


def test_empty_is_false():
    assert is_introspective("") is False
    assert is_introspective("   ") is False


# --- the factual "what model are you on" stays a canned query (NOT pre-empted) -----------------------

def test_factual_model_query_still_canned():
    assert is_introspective("what model are you on") is False
    mc = detect_meta_command("what model are you on")
    assert mc is not None and mc.kind == "query" and mc.value == "model"


# --- disclosure: packaged doc must name no internal identifiers --------------------------------------

FORBIDDEN_TOKENS = [
    "arya", "haiku", "devstral", "sonnet", "opus", "gab", "jellyfin", "kokoro", "mopidy", "tidal",
    "localhost", "8765", "8766", "8770", "8771", "respeaker", "pipecat",
]


def test_packaged_doc_has_no_internal_identifiers():
    doc = about_text().lower()
    assert doc, "packaged doc should be non-empty"
    for tok in FORBIDDEN_TOKENS:
        assert tok not in doc, f"self-knowledge doc leaks internal identifier {tok!r}"


# --- override precedence + size cap ------------------------------------------------------------------

def test_override_precedence_and_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("gabagent.introspect.knowledge.data_dir", lambda: tmp_path)
    d = tmp_path / "selfknowledge"
    d.mkdir(parents=True)
    # short override wins over packaged
    (d / "about.md").write_text("CUSTOM SELF KNOWLEDGE", encoding="utf-8")
    assert about_text() == "CUSTOM SELF KNOWLEDGE"
    # oversize override is capped
    (d / "about.md").write_text("x" * (_OVERRIDE_CAP + 500), encoding="utf-8")
    assert len(about_text()) <= _OVERRIDE_CAP


def test_packaged_used_when_no_override(tmp_path, monkeypatch):
    monkeypatch.setattr("gabagent.introspect.knowledge.data_dir", lambda: tmp_path)
    assert "how i work" in about_text().lower()


# --- introspect_brief gating -------------------------------------------------------------------------

class _Msg:
    def __init__(self, role, content):
        self.role = role
        self.content = content


class _Session:
    def __init__(self, msgs):
        self._m = msgs

    def messages(self):
        return list(self._m)


def _ctx(msgs):
    return types.SimpleNamespace(session=_Session(msgs))


def test_brief_injects_on_introspective_last_user_turn():
    ctx = _ctx([_Msg("user", "how do you decide which model to use")])
    out = introspect_brief(ctx)
    assert out and "how i work" in out.lower()
    assert "ONE short" in out  # the terseness guard framing is present


def test_brief_empty_on_normal_turn():
    ctx = _ctx([_Msg("user", "what time is it")])
    assert introspect_brief(ctx) == ""


def test_brief_scans_back_past_assistant_tool_messages():
    # M1: on a tool-loop round the last message is assistant/tool, but the user turn was introspective.
    ctx = _ctx([
        _Msg("user", "what are your limits"),
        _Msg("assistant", "calling a tool"),
        _Msg("tool", "tool result"),
    ])
    assert introspect_brief(ctx) != ""


def test_brief_empty_when_last_user_is_not_introspective():
    ctx = _ctx([
        _Msg("user", "play some jazz"),
        _Msg("assistant", "ok"),
    ])
    assert introspect_brief(ctx) == ""


def test_brief_defensive_on_bad_session():
    assert introspect_brief(types.SimpleNamespace(session=None)) == ""
