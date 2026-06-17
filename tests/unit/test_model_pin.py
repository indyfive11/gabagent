"""Runtime model pin: resolve a name → (model, effort, backend); voice meta detection."""
import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.agent.router import resolve_pin
from gabagent.voice.commands import detect_meta_command


def _cfg():
    cfg = GabAgentConfig()
    cfg.local_model = "devstral:24b"
    return cfg


@pytest.mark.parametrize("text,expect", [
    ("opus", ("claude-opus-4-8", "high", "claude")),
    ("opus max", ("claude-opus-4-8", "max", "claude")),
    ("sonnet", ("claude-sonnet-4-6", "high", "claude")),
    ("haiku", ("claude-haiku-4-5", "", "claude")),
    ("aria", ("arya", None, "gab")),
    ("arya", ("arya", None, "gab")),
    ("local", ("devstral:24b", None, "local")),
    ("devstral", ("devstral:24b", None, "local")),
    ("claude-opus-4-8", ("claude-opus-4-8", "high", "claude")),
    ("claude-opus-4-8 xhigh", ("claude-opus-4-8", "xhigh", "claude")),
])
def test_resolve_pin(text, expect):
    assert resolve_pin(text, _cfg()) == expect


@pytest.mark.parametrize("text", ["bogus", "gpt-4", ""])
def test_resolve_pin_unknown(text):
    assert resolve_pin(text, _cfg()) is None


def test_resolve_local_none_when_unconfigured():
    cfg = GabAgentConfig()  # no local_model
    assert resolve_pin("local", cfg) is None


@pytest.mark.parametrize("text,val", [
    ("just use opus", "opus"),
    ("only use sonnet", "sonnet"),
    ("lock to haiku", "haiku"),
    ("pin opus at max", "opus max"),
    ("use only aria", "aria"),
    ("stick with opus", "opus"),
])
def test_pin_meta_detected(text, val):
    mc = detect_meta_command(text)
    assert mc is not None and mc.kind == "pin" and mc.value == val


@pytest.mark.parametrize("text", [
    "back to auto", "use automatic", "use the ladder", "unpin", "stop pinning", "resume escalation",
])
def test_unpin_meta_detected(text):
    mc = detect_meta_command(text)
    assert mc is not None and mc.kind == "pin" and mc.value == "auto"


@pytest.mark.parametrize("text,kind,val", [
    ("use aria", "brain", "cloud"),       # bare switch, NOT a pin
    ("switch to local", "brain", "local"),
    ("make local the floor", "floor", "local"),
])
def test_pin_does_not_eat_other_commands(text, kind, val):
    mc = detect_meta_command(text)
    assert mc is not None and mc.kind == kind and mc.value == val


def test_pin_friendly_names():
    from gabagent.voice.turn import _pin_friendly
    assert _pin_friendly("claude-opus-4-8") == "Opus"
    assert _pin_friendly("claude-sonnet-4-6") == "Sonnet"
    assert _pin_friendly("arya") == "Aria"
