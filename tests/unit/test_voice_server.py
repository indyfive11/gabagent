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

    async def stream_complete(self, messages, tools=None, model=None, retry_model=None, **kw):
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


async def test_builder_poll_drains_and_acks(tmp_path, monkeypatch):
    # The proactive channel endpoint: a GET drains deferred announcements for the polling session, and a
    # piggybacked ?ack= finalizes a previously-spoken item. (Redirect XDG so the store writes under tmp.)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    from gabagent.voice import announce_store
    announce_store.enqueue("Builder job done.", job_id="b1", source="builder")
    app = build_app(make_ctx(tmp_path, []))
    async with _client(app) as client:
        r = await client.get("/builder/poll", params={"session_id": "s1"})
        assert r.status_code == 200
        assert r.json()["deferred"] == [{"job_id": "b1", "text": "Builder job done."}]
        # Ack on the next poll removes it; nothing left to deliver.
        r2 = await client.get("/builder/poll", params={"session_id": "s1", "ack": "b1"})
        assert r2.json()["deferred"] == []
        assert announce_store.pending() == []


async def test_builder_poll_is_bearer_guarded(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    ctx = make_ctx(tmp_path, [])
    ctx.config.voice_auth_token = "tok"
    app = build_app(ctx)
    async with _client(app) as client:
        assert (await client.get("/builder/poll", params={"session_id": "s"})).status_code == 401


async def test_respond_logs_received_marker_and_turn_lifecycle(tmp_path):
    # The `respond_recv` marker (fires even on a busy 409) + turn_start/turn_done bracket every turn, so a
    # future freeze is localised: no respond_recv after a wake = voice never reached us; turn_start with no
    # turn_done = the turn itself hung. (Diagnoses the close-movie freeze class.)
    ctx = make_ctx(tmp_path, [["All done."]])           # no tools → quick done
    ctx.voice_debug_path = tmp_path / "voice_debug.jsonl"
    app = build_app(ctx)
    async with _client(app) as client:
        async with client.stream("POST", "/respond", json={"session_id": "s1", "text": "hello there"}) as resp:
            await _drain(resp)
    rows = []
    for _ in range(50):                                  # turn_done fires in the task's finally, post-done
        rows = [json.loads(l) for l in ctx.voice_debug_path.read_text().splitlines() if l.strip()]
        if any(r["event"] == "turn_done" for r in rows):
            break
        await asyncio.sleep(0.02)
    rr = next(r for r in rows if r["event"] == "respond_recv")
    assert rr["busy"] is False and rr["words"] == 2
    assert "turn_start" in {r["event"] for r in rows}
    assert "dur_ms" in next(r for r in rows if r["event"] == "turn_done")


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


@pytest.mark.parametrize("body_wake,expected", [
    ({"bare_wake_likelihood": 0.9, "confidence": 0.97}, {"bare_wake_likelihood": 0.9, "confidence": 0.97}),
    ("not-a-dict", None),     # item C: a stray non-object is dropped, never perturbs the turn
    (None, None),             # absent => no signal
])
async def test_respond_threads_wake_signal_to_start_turn(tmp_path, monkeypatch, body_wake, expected):
    # /respond parses the optional out-of-band `wake` object and threads it (dict-only) into start_turn.
    import gabagent.voice.turn as turnmod
    real = turnmod.start_turn
    captured = {}

    def spy(ctx, vs, text, wake=None):
        captured["wake"] = wake
        return real(ctx, vs, text, wake=wake)

    monkeypatch.setattr(turnmod, "start_turn", spy)  # lazy-imported in build_app → picks up the spy
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    proj = tmp_path / "proj"
    proj.mkdir()
    app = build_app(make_ctx(proj, [["Done."]]))
    payload = {"session_id": "s1", "text": "how are you"}
    if body_wake is not None:
        payload["wake"] = body_wake
    async with _client(app) as client:
        async with client.stream("POST", "/respond", json=payload) as resp:
            await _drain(resp)
    assert captured["wake"] == expected


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


async def test_media_state_bot_speaking_refreshes_duck_watchdog(tmp_path):
    """`bot_speaking=true` on the ~1 Hz /media/state poll refreshes the duck-watchdog heartbeat, so a long
    bot reply (a story — no INCOMING utterance) can't trip the watchdog and pop the bed mid-reply. An absent
    or `false` param is a plain check with no refresh (back-compat with an older voice side)."""
    import gabagent.voice.ducking as _dk
    ctx = make_ctx(tmp_path, [])
    app = build_app(ctx)
    st = _dk._state(ctx)
    async with _client(app) as client:
        st["last_duck_refresh_mono"] = 1.0                       # ancient heartbeat
        await client.get("/media/state", params={"bot_speaking": "true"})
        assert st["last_duck_refresh_mono"] > 1.0               # refreshed while she's speaking

        st["last_duck_refresh_mono"] = 1.0
        await client.get("/media/state")                        # no param ⇒ plain check, no refresh
        assert st["last_duck_refresh_mono"] == 1.0

        st["last_duck_refresh_mono"] = 1.0
        await client.get("/media/state", params={"bot_speaking": "false"})
        assert st["last_duck_refresh_mono"] == 1.0


async def test_no_auth_token_leaves_endpoints_open(tmp_path):
    # Default (loopback, empty token) => no guard installed, every endpoint reachable with no header.
    ctx = make_ctx(tmp_path, [])
    assert ctx.config.voice_auth_token == ""
    app = build_app(ctx)
    async with _client(app) as client:
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/media/state")).status_code == 200


async def test_bearer_token_required_when_configured(tmp_path):
    # A configured token guards every endpoint except /health: missing/wrong => 401, correct => through.
    ctx = make_ctx(tmp_path, [])
    ctx.config.voice_auth_token = "s3cret"
    app = build_app(ctx)
    async with _client(app) as client:
        # /health stays an open liveness probe.
        assert (await client.get("/health")).status_code == 200
        # No Authorization header => 401.
        r = await client.get("/media/state")
        assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
        # Wrong token => 401.
        assert (await client.get(
            "/media/state", headers={"Authorization": "Bearer wrong"})).status_code == 401
        # A bare token without the Bearer scheme => 401.
        assert (await client.get(
            "/media/state", headers={"Authorization": "s3cret"})).status_code == 401
        # Correct bearer => through to the handler.
        assert (await client.get(
            "/media/state", headers={"Authorization": "Bearer s3cret"})).status_code == 200


async def test_bearer_token_guards_respond_post(tmp_path):
    # The guard covers the mutating POST endpoints too, not just the GET probes.
    ctx = make_ctx(tmp_path, [["Done."]])
    ctx.config.voice_auth_token = "tok"
    app = build_app(ctx)
    async with _client(app) as client:
        r = await client.post("/respond", json={"session_id": "s", "text": "hi"})
        assert r.status_code == 401
        # With the bearer the turn streams (drains to a done event).
        async with client.stream(
            "POST", "/respond",
            headers={"Authorization": "Bearer tok"},
            json={"session_id": "s", "text": "hi"},
        ) as resp:
            assert resp.status_code == 200
            events = await _drain(resp)
        assert any(e["type"] == "done" for e in events)


# --- STT-offload / multi-room foundation: /attach + optional room_id ---------------------------

async def test_attach_handshake_stashes_room_and_returns_brain_caps(tmp_path):
    # /attach registers the room + client capabilities on the session and returns the brain's own
    # capability advertisement (bidirectional handshake), so the client negotiates instead of assuming.
    from gabagent import __version__
    app = build_app(make_ctx(tmp_path, []))
    async with _client(app) as client:
        r = await client.post("/attach", json={
            "session_id": "s1", "room_id": "kitchen",
            "capabilities": {"wake": True, "vad": True, "stt": "remote", "tts": True},
        })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["brain"] == {"version": __version__, "accepts_room_id": True}
    vs = app.state.sessions["s1"]
    assert vs.room_id == "kitchen"
    assert vs.capabilities == {"wake": True, "vad": True, "stt": "remote", "tts": True}


async def test_attach_is_idempotent_and_tolerant(tmp_path):
    # Re-callable (reconnect/restart) and never 400s on missing/garbage fields: a non-dict capabilities
    # defaults to {}, an absent/garbage room_id => None — a newer client can't break an older brain.
    app = build_app(make_ctx(tmp_path, []))
    async with _client(app) as client:
        r1 = await client.post("/attach", json={
            "session_id": "s1", "room_id": "office", "capabilities": "not-a-dict",
        })
        assert r1.status_code == 200
        assert app.state.sessions["s1"].room_id == "office"
        assert app.state.sessions["s1"].capabilities == {}     # garbage caps defaulted
        # Re-attach (no room_id this time) => no error; capabilities upsert.
        r2 = await client.post("/attach", json={
            "session_id": "s1", "capabilities": {"stt": "remote"},
        })
        assert r2.status_code == 200
        assert app.state.sessions["s1"].capabilities == {"stt": "remote"}
        # room_id omitted on the re-attach must NOT clobber the prior value.
        assert app.state.sessions["s1"].room_id == "office"


async def test_attach_guarded_by_bearer(tmp_path):
    # /attach is a non-/health endpoint, so the configured token guards it like the rest.
    ctx = make_ctx(tmp_path, [])
    ctx.config.voice_auth_token = "tok"
    app = build_app(ctx)
    async with _client(app) as client:
        assert (await client.post("/attach", json={"session_id": "s"})).status_code == 401
        ok = await client.post(
            "/attach", headers={"Authorization": "Bearer tok"},
            json={"session_id": "s", "room_id": "den"},
        )
        assert ok.status_code == 200 and ok.json()["status"] == "ok"


async def test_respond_threads_room_id_onto_session_refresh_only(tmp_path):
    # /respond stashes a payload room_id on the session (multi-room routing key), but an omitted room_id
    # must NOT clobber a value an earlier /attach set (refresh-only semantics).
    app = build_app(make_ctx(tmp_path, [["Done."], ["Done again."]]))
    async with _client(app) as client:
        await client.post("/attach", json={"session_id": "s1", "room_id": "bedroom"})
        async with client.stream("POST", "/respond",
                                 json={"session_id": "s1", "text": "hi"}) as resp:
            await _drain(resp)
        assert app.state.sessions["s1"].room_id == "bedroom"     # preserved (no room_id in payload)
        async with client.stream("POST", "/respond",
                                 json={"session_id": "s1", "text": "hi", "room_id": "garage"}) as resp:
            await _drain(resp)
        assert app.state.sessions["s1"].room_id == "garage"      # refreshed from the payload
