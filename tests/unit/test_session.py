import pytest
from pathlib import Path
from gabagent.session.serializer import SessionFile
from gabagent.api.models import ChatMessage


def test_append_and_reload(tmp_path):
    path = tmp_path / "session.jsonl"
    sf = SessionFile(path)
    sf.append_message(ChatMessage(role="user", content="hello"))
    sf.append_message(ChatMessage(role="assistant", content="world"))

    sf2 = SessionFile(path)
    msgs = sf2.messages()
    assert len(msgs) == 2
    assert msgs[0].role == "user"
    assert msgs[0].content == "hello"
    assert msgs[1].role == "assistant"


def test_empty_session(tmp_path):
    path = tmp_path / "session.jsonl"
    path.touch()
    sf = SessionFile(path)
    assert sf.messages() == []


def test_replace_all(tmp_path):
    path = tmp_path / "session.jsonl"
    sf = SessionFile(path)
    sf.append_message(ChatMessage(role="user", content="old"))
    new_msgs = [ChatMessage(role="system", content="summary")]
    sf.replace_all(new_msgs)

    sf2 = SessionFile(path)
    msgs = sf2.messages()
    assert len(msgs) == 1
    assert msgs[0].role == "system"
    assert msgs[0].content == "summary"
