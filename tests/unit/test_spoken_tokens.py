"""Spoken → token assembly (brain owns spoken-path/punctuation assembly)."""
import pytest

from gabagent.voice.spoken_tokens import (
    assemble_path, looks_spoken, maybe_assemble_path, normalize_name,
)


@pytest.mark.parametrize("spoken,expected", [
    ("slash tmp slash builder dash test", "/tmp/builder-test"),
    ("hello dot txt", "hello.txt"),
    ("slash home slash rob slash notes dot md", "/home/rob/notes.md"),
    ("slash tmp slash my underscore app", "/tmp/my_app"),
])
def test_assemble_path(spoken, expected):
    assert assemble_path(spoken) == expected


def test_looks_spoken_gate():
    assert looks_spoken("slash tmp slash x")
    assert not looks_spoken("/tmp/builder-test")     # already clean → no marker
    assert not looks_spoken("hello.txt")


def test_maybe_assemble_leaves_clean_paths():
    # A clean path is untouched (no "slash" word) — even though it contains dots/dashes.
    assert maybe_assemble_path("/tmp/builder-test") == "/tmp/builder-test"
    assert maybe_assemble_path("hello.txt") == "hello.txt"
    assert maybe_assemble_path("slash tmp slash x dot py") == "/tmp/x.py"


@pytest.mark.parametrize("spoken,expected", [
    ("snake dash game", "snake-game"),
    ("my underscore project", "my_project"),
    ("config dot json", "config.json"),
    ("snake game", "snake game"),          # plain words → untouched (slugify handles spaces)
    ("dash", "dash"),                       # a BARE separator word is a literal name, not eaten
    ("dot", "dot"),
    ("dashboard", "dashboard"),             # word boundary: 'dash' inside 'dashboard' is safe
])
def test_normalize_name(spoken, expected):
    assert normalize_name(spoken) == expected


def test_normalize_name_then_slugify():
    from gabagent.builder.projects import slugify
    assert slugify(normalize_name("snake dash game")) == "snake-game"
    # without normalization the verbalized dash would survive as a word
    assert slugify("snake dash game") == "snake-dash-game"


import pytest as _pytest


@_pytest.mark.parametrize("spoken,expected", [
    ("Pitch Perfect to", "Pitch Perfect 2"),    # the wife's case: homophone two→to
    ("pitch perfect two", "pitch perfect 2"),
    ("Toy Story three", "Toy Story 3"),
    ("Ocean's eight", "Ocean's 8"),
    ("Rocky won", "Rocky 1"),
    ("the matrix", "the matrix"),               # no trailing number word → untouched
    ("two", "two"),                             # bare number word (no preceding title) → untouched
    ("what are you waiting for", "what are you waiting 4"),  # mapped, but library-guard rejects downstream
])
def test_normalize_trailing_number(spoken, expected):
    from gabagent.voice.spoken_tokens import normalize_trailing_number
    assert normalize_trailing_number(spoken) == expected
