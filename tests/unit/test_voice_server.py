import asyncio
import json
import types
from pathlib import Path
import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("starlette")

from gabagent.api.models import ToolCallSpec
from gabagent.config.models import GabAgentConfig
from gabagent.agent.context import AgentContext
from gabagent.permissions.voice_approve import voice_approve
from gabagent.voice.server import build_app
import gabagent.tools.file_tools  # noqa: F401


class FakeSession:
    def __init__(self):
        self._msgs = []

    def messages(self):
        return list(self._msgs)

    def append_message(self, m):
        self._msgs.append(m)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.model = "arya"

    async def stream_complete(self, messages, tools=None, model=None):
        for c in self.responses.pop(0):
            yield c

    async def complete_simple(self, messages, model=None):
        return "[SIMPLE]"


def _spec(name, **args):
    return ToolCallSpec(id=name + "-1", name=name, arguments=json.dumps(args))


def make_ctx(tmp_path, responses):
    cfg = GabAgentConfig(api_key="test")
    cfg.router.enabled = False
    ctx = AgentContext(
        config=cfg,
        client=FakeClient(responses),
        rate_limiter=types.SimpleNamespace(record=lambda *a, **k: None),
        session=FakeSession(),
        session_id="s",
        cwd=tmp_path,
        system_prompt="",
        headless=True,
    )
    ctx.voice_mode = True
    ctx.approval_hook = voice_approve
    return ctx


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _drain(resp):
    """Collect SSE events until the stream ends at a confirm or done."""
    events = []
    async for line in resp.aiter_lines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        ev = json.loads(line[len("data:"):].strip())
        events.append(ev)
        if ev["type"] in ("confirm", "done"):
            break
    return events


async def test_health(tmp_path):
    app = build_app(make_ctx(tmp_path, []))
    async with _client(app) as client:
        r = await client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "mode": "voice"}


async def test_respond_streams_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()
    dest = tmp_path / "voice-scratch" / "note.txt"
    app = build_app(make_ctx(proj, [
        ["Sure. ", "Writing it.", [_spec("write_file", path=str(dest), content="hello")]],
        ["Saved."],
    ]))
    async with _client(app) as client:
        async with client.stream("POST", "/respond", json={"session_id": "s1", "text": "note"}) as resp:
            events = await _drain(resp)
    types_seen = {e["type"] for e in events}
    assert "token" in types_seen and "done" in types_seen
    assert "confirm" not in types_seen
    assert dest.read_text() == "hello"


async def test_respond_confirm_returns_continuation(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()
    target = proj / "readme.txt"
    target.write_text("old\n")
    app = build_app(make_ctx(proj, [
        [[_spec("edit", path=str(target), old_string="old", new_string="new")]],
        ["Done."],
    ]))
    async with _client(app) as client:
        # Phase 1: /respond streams up to the confirm, then the SSE ends.
        async with client.stream("POST", "/respond", json={"session_id": "s1", "text": "edit it"}) as resp:
            first = await _drain(resp)
        confirm = next(e for e in first if e["type"] == "confirm")
        assert confirm["method"] == "spoken_yesno" and confirm["tier"] == 2
        assert target.read_text() == "old\n"  # not yet applied

        # Phase 2: /confirm returns the continuation as a fresh SSE.
        async with client.stream(
            "POST", "/confirm",
            json={"session_id": "s1", "id": confirm["id"], "approved": True},
        ) as resp2:
            second = await _drain(resp2)
    assert any(e["type"] == "done" for e in second)
    assert target.read_text() == "new\n"


async def test_respond_rejects_concurrent_turn(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()
    target = proj / "readme.txt"
    target.write_text("old\n")
    app = build_app(make_ctx(proj, [
        [[_spec("edit", path=str(target), old_string="old", new_string="new")]],
        ["Done."],
    ]))
    async with _client(app) as client:
        # Leave a turn paused at its confirm, then start a second /respond.
        async with client.stream("POST", "/respond", json={"session_id": "s1", "text": "edit it"}) as resp:
            await _drain(resp)
        r = await client.post("/respond", json={"session_id": "s1", "text": "again"})
        assert r.status_code == 409


async def test_confirm_after_turn_done_is_409(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()
    app = build_app(make_ctx(proj, [["All done."]]))  # no tools -> quick done
    async with _client(app) as client:
        async with client.stream("POST", "/respond", json={"session_id": "s1", "text": "hi"}) as resp:
            await _drain(resp)
        await asyncio.sleep(0.01)  # let the turn task finish
        r = await client.post("/confirm", json={"session_id": "s1", "id": "x", "approved": True})
    assert r.status_code == 409


async def test_confirm_unknown_session(tmp_path):
    app = build_app(make_ctx(tmp_path, []))
    async with _client(app) as client:
        r = await client.post("/confirm", json={"session_id": "nope", "id": "x", "approved": True})
    assert r.status_code == 404


async def test_cancel_unknown_session(tmp_path):
    app = build_app(make_ctx(tmp_path, []))
    async with _client(app) as client:
        r = await client.post("/cancel", json={"session_id": "nope"})
    assert r.status_code == 404
