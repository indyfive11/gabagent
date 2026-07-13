"""Layer-A wizard primitives — pure stdlib, driven by monkeypatching input()."""
from __future__ import annotations

import builtins

import pytest

from installkit import wizard


def _feed(monkeypatch, answers):
    """Make input() return each of `answers` in turn (then raise if over-consumed)."""
    it = iter(answers)

    def fake_input(_prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise AssertionError("input() called more times than answers provided")

    monkeypatch.setattr(builtins, "input", fake_input)


def test_prompt_returns_value(monkeypatch):
    _feed(monkeypatch, ["hello"])
    assert wizard.prompt("x") == "hello"


def test_prompt_blank_takes_default(monkeypatch):
    _feed(monkeypatch, [""])
    assert wizard.prompt("x", default="def") == "def"


def test_prompt_strips_whitespace(monkeypatch):
    _feed(monkeypatch, ["  spaced  "])
    assert wizard.prompt("x") == "spaced"


def test_prompt_eof_with_default_returns_default(monkeypatch):
    def raise_eof(_p=""):
        raise EOFError
    monkeypatch.setattr(builtins, "input", raise_eof)
    assert wizard.prompt("x", default="fallback") == "fallback"


def test_prompt_eof_without_default_raises_cancelled(monkeypatch):
    def raise_eof(_p=""):
        raise EOFError
    monkeypatch.setattr(builtins, "input", raise_eof)
    with pytest.raises(wizard.WizardCancelled):
        wizard.prompt("x")


def test_choose_returns_zero_based_index(monkeypatch):
    _feed(monkeypatch, ["2"])
    assert wizard.choose("pick", ["a", "b", "c"]) == 1


def test_choose_blank_takes_default_index(monkeypatch):
    _feed(monkeypatch, [""])
    assert wizard.choose("pick", ["a", "b", "c"], default_index=2) == 2


def test_choose_reprompts_on_bad_input(monkeypatch):
    _feed(monkeypatch, ["9", "notanumber", "1"])
    assert wizard.choose("pick", ["a", "b"]) == 0


def test_choose_empty_options_raises():
    with pytest.raises(ValueError):
        wizard.choose("pick", [])


def test_confirm_default_true_on_blank(monkeypatch):
    _feed(monkeypatch, [""])
    assert wizard.confirm("ok?", default=True) is True


def test_confirm_default_false_on_blank(monkeypatch):
    _feed(monkeypatch, [""])
    assert wizard.confirm("ok?", default=False) is False


def test_confirm_yes(monkeypatch):
    _feed(monkeypatch, ["y"])
    assert wizard.confirm("ok?", default=False) is True


def test_confirm_no(monkeypatch):
    _feed(monkeypatch, ["n"])
    assert wizard.confirm("ok?", default=True) is False


def test_confirm_eof_takes_default_not_fatal(monkeypatch):
    def raise_eof(_p=""):
        raise EOFError
    monkeypatch.setattr(builtins, "input", raise_eof)
    assert wizard.confirm("ok?", default=True) is True


def test_save_confirm_uses_path(monkeypatch):
    _feed(monkeypatch, ["y"])
    assert wizard.save_confirm("/tmp/x.json", default=False) is True


def test_supports_color_false_when_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert wizard.supports_color() is False
