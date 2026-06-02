"""Tests for the voice error-handling / speakable-confirm fixes (VOICE_MODE_DEBUG_jellyfin)."""
import json
import types
from pathlib import Path
import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.agent.context import AgentContext
from gabagent.voice.session import VoiceSession
from gabagent.voice import events
from gabagent.voice.commands import detect_meta_command, answer_query
from gabagent.voice.turn import start_turn, drain, _status_phrase
from gabagent.permissions.voice_approve import _summarize
from gabagent.permissions.voice_approve import voice_approve
from gabagent.commands.model import Command, Slot, BrowserBackend
from gabagent.commands.catalog import CommandCatalog
from gabagent.api.models import ToolCallSpec


def _ctx(tmp_path, **kw):
    cfg = GabAgentConfig(api_key="test")
    ctx = types.SimpleNamespace(
        config=cfg, cwd=tmp_path, local_mode=False,
        voice_emit=None, voice_session=None, voice_audit_path=None,
        command_catalog=None,
    )
    for k, v in kw.items():
        setattr(ctx, k, v)
    return ctx


def _play_cmd():
    return Command(
        id="jellyfin.play", domain="media", tier=2,
        summary="Play a movie — on your open client if you have one, or in a new window",
        confirm_template="Play {title} in Jellyfin?",
        backend=BrowserBackend(ref="x:y"),
        params=[Slot("item_id", "string", True), Slot("title", "string", False)],
    )


# -- #3 speakable confirm summary -----------------------------------------

def test_summarize_run_command_uses_title_template(tmp_path):
    cat = CommandCatalog(); cat.add(_play_cmd())
    ctx = _ctx(tmp_path, command_catalog=cat)
    s = _summarize("run_command",
                   {"command_id": "jellyfin.play", "args": {"item_id": "abc", "title": "The Matrix"}}, ctx)
    assert s == "Play The Matrix in Jellyfin?"
    assert "{" not in s and "command_id" not in s and "abc" not in s


def test_summarize_run_command_falls_back_to_summary_without_title(tmp_path):
    cat = CommandCatalog(); cat.add(_play_cmd())
    ctx = _ctx(tmp_path, command_catalog=cat)
    s = _summarize("run_command", {"command_id": "jellyfin.play", "args": {"item_id": "abc"}}, ctx)
    assert s.startswith("Play a movie")
    assert "{" not in s and "command_id" not in s


def test_summarize_run_command_unknown_is_clean(tmp_path):
    # Unknown ids are humanized (namespace + underscores stripped), never read like a raw id.
    ctx = _ctx(tmp_path, command_catalog=CommandCatalog())
    s = _summarize("run_command", {"command_id": "media.play_movie", "args": {}}, ctx)
    assert s == "Run play movie." and "_" not in s and "." not in s.rstrip(".")
    ctx2 = _ctx(tmp_path, command_catalog=None)
    assert _summarize("run_command", {"command_id": "x.y"}, ctx2) == "Run y."


def test_real_jellyfin_play_has_template():
    from gabagent.commands.providers.jellyfin import JellyfinProvider
    cmds = {c.id: c for c in JellyfinProvider().commands(ctx=None)}
    play = cmds["jellyfin.play"]
    assert play.confirm_template == "Play {title} in Jellyfin?"
    assert any(s.name == "title" and not s.required for s in play.params)


# -- spoken polish: every summary is TTS-ready -----------------------------

def test_summarize_memory_write_is_speakable(tmp_path):
    ctx = _ctx(tmp_path)
    s = _summarize("memory_write", {"content": "line one\nline two\nline three"}, ctx)
    assert "\n" not in s and "(" not in s and "content=" not in s
    assert s == "Save a note to your project memory."


def test_summarize_unknown_tool_never_dumps_args(tmp_path):
    ctx = _ctx(tmp_path)
    s = _summarize("frobnicate_widget", {"payload": "x\ny", "secret": "z"}, ctx)
    assert "\n" not in s and "payload" not in s and "secret" not in s
    assert s == "Run the frobnicate widget action."


def test_summarize_commit_message_flattened(tmp_path):
    ctx = _ctx(tmp_path)
    s = _summarize("git_commit", {"message": "title\n\nbody line\nmore"}, ctx)
    assert "\n" not in s


def test_reason_tier3_carries_detail_for_dialog(tmp_path):
    from gabagent.permissions.voice_approve import _reason
    r = _reason("memory_write", {"content": "Buy milk and eggs"}, 3, _ctx(tmp_path))
    assert "Buy milk and eggs" in r           # shown in the keyboard dialog body
    # ...but the spoken summary stays generic
    assert "Buy milk" not in _summarize("memory_write", {"content": "Buy milk and eggs"}, _ctx(tmp_path))


def test_app_open_url_specific_confirm(tmp_path):
    cat = CommandCatalog()
    cat.add(Command(id="app.open_url", domain="apps", tier=2,
                    summary="Open a URL in the default browser",
                    confirm_template="Open {url} in your browser?",
                    backend=BrowserBackend(ref="x:y"), params=[Slot("url", "string", True)]))
    ctx = _ctx(tmp_path, command_catalog=cat)
    s = _summarize("run_command", {"command_id": "app.open_url", "args": {"url": "google.com"}}, ctx)
    assert s == "Open google.com in your browser?"


# -- #2 "what went wrong?" reports, doesn't retry --------------------------

def test_detect_error_query():
    for phrase in ("what was the error", "what went wrong", "what happened",
                   "why did that fail", "what's the error"):
        m = detect_meta_command(phrase)
        assert m is not None and m.kind == "query" and m.value == "error", phrase


def test_error_query_no_false_positive():
    assert detect_meta_command("what does this function do") is None
    assert detect_meta_command("open the local file") is None


def test_answer_query_error(tmp_path):
    vs = VoiceSession("s", None)
    ctx = _ctx(tmp_path, voice_session=vs)
    assert "nothing" in answer_query(ctx, "error").lower()
    vs.set_error("RuntimeError: boom")
    assert "RuntimeError: boom" in answer_query(ctx, "error")


# -- error event ----------------------------------------------------------

def test_error_event_round_trips_sse():
    ev = events.error("TimeoutError: slow", "Sorry, I hit a problem.")
    d = json.loads(ev.sse().removeprefix("data: ").strip())
    assert d["type"] == "error"
    assert d["text"] == "Sorry, I hit a problem."
    assert d["summary"] == "TimeoutError: slow"


# -- confirm phrasing contract (Option A + flag) --------------------------

def test_confirm_default_is_option_a():
    ev = events.confirm("c1", 2, "spoken_yesno", "Edit one section of readme.")
    assert ev.prompt_is_complete is False
    assert "prompt_is_complete" not in ev.to_dict()   # False omitted → client appends the tail


def test_confirm_complete_sets_flag():
    ev = events.confirm("c2", 2, "spoken_yesno",
                        "Play The Matrix on your open Chrome? Say yes to play there, or no to open a new window.",
                        "", prompt_complete=True)
    d = ev.to_dict()
    assert d["prompt_is_complete"] is True            # present only when complete


# -- #4 varied status -----------------------------------------------------

def test_status_phrase_varies_by_family():
    def tc(name, **a):
        return ToolCallSpec(id=name, name=name, arguments=json.dumps(a))
    assert _status_phrase([tc("write_file", path="x")]) == "Making that change."
    assert _status_phrase([tc("list_capabilities")]) == ""          # recon is silent now
    assert _status_phrase([tc("rescan_capabilities")]) == ""
    assert _status_phrase([tc("grep", pattern="x")]) == "Reading through things."
    assert "Jellyfin" in _status_phrase([tc("run_command", command_id="jellyfin.play")])
    assert _status_phrase([tc("some_unknown_tool")]) == "Looking into it."


# -- #1 turn-level error capture ------------------------------------------

class _RaisingClient:
    model = "arya"

    async def stream_complete(self, messages, tools=None, model=None, retry_model=None, **kw):
        raise RuntimeError("kaboom")
        yield  # noqa: unreachable — makes this an async generator

    async def complete_simple(self, messages, model=None):
        return "x"


class _FakeSession:
    def __init__(self): self._m = []
    def messages(self): return list(self._m)
    def append_message(self, m): self._m.append(m)


async def test_turn_error_is_captured(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    cfg = GabAgentConfig(api_key="test")
    cfg.router.enabled = False
    debug_path = tmp_path / "voice_debug.jsonl"
    ctx = AgentContext(
        config=cfg, client=_RaisingClient(),
        rate_limiter=types.SimpleNamespace(record=lambda *a, **k: None),
        session=_FakeSession(), session_id="s", cwd=tmp_path,
        system_prompt="", headless=True,
    )
    ctx.voice_mode = True
    ctx.voice_session = VoiceSession("s", ctx)
    ctx.voice_debug_path = str(debug_path)

    vs = ctx.voice_session
    evs = []
    start_turn(ctx, vs, "open a jellyfin movie")
    async for ev in drain(vs):
        evs.append(ev)

    # Structured error event (carries a speakable text + structured cause) + terminal done.
    err = [e for e in evs if e.type == "error"]
    assert err and "kaboom" in err[0].summary and err[0].text
    assert evs[-1].type == "done"
    # Stored for "what went wrong?".
    assert vs.last_error and "kaboom" in vs.last_error
    # Logged to the debug join file.
    rows = [json.loads(l) for l in debug_path.read_text().splitlines()]
    assert any(r["event"] == "error" and "kaboom" in r["cause"] for r in rows)
