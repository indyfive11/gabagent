import json
import types
from pathlib import Path
import pytest

from gabagent.api.models import ChatMessage, ToolCallSpec
from gabagent.config.models import GabAgentConfig
from gabagent.agent.context import AgentContext
from gabagent.voice.session import VoiceSession
from gabagent.voice.turn import voice_turn
from gabagent.permissions.voice_approve import voice_approve
import gabagent.tools.file_tools  # noqa: F401  (registers read_file/write_file/edit)


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
        chunks = self.responses.pop(0)
        for c in chunks:
            yield c

    async def complete_simple(self, messages, model=None):
        return "[SIMPLE]"


def _spec(name, **args):
    return ToolCallSpec(id=name + "-1", name=name, arguments=json.dumps(args))


def make_ctx(tmp_path, responses, **cfg_kw):
    cfg = GabAgentConfig(api_key="test", **cfg_kw)
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
    ctx.voice_session = VoiceSession("s", ctx)
    return ctx


async def run_turn(ctx, text, answer=None):
    evs = []
    async for ev in voice_turn(ctx, text):
        evs.append(ev)
        if ev.type == "confirm" and answer is not None:
            ctx.voice_session.resolve(ev.id, answer)
    return evs


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


async def test_tier1_scratch_write_streams_and_writes(home):
    proj = home / "proj"
    proj.mkdir()
    dest = home / "voice-scratch" / "hello.txt"
    ctx = make_ctx(proj, [
        ["Okay. ", "On it.", [_spec("write_file", path=str(dest), content="hi there")]],
        ["Saved it."],
    ])
    evs = await run_turn(ctx, "draft a hello note")
    assert not any(e.type == "confirm" for e in evs)          # Tier 1, no gate
    assert any(e.type == "token" for e in evs)
    assert any(e.type == "done" for e in evs)
    assert dest.read_text() == "hi there"


async def test_tier2_edit_requires_spoken_confirm(home):
    proj = home / "proj"
    proj.mkdir()
    target = proj / "readme.txt"
    target.write_text("line one\n")
    ctx = make_ctx(proj, [
        [[_spec("edit", path=str(target), old_string="line one", new_string="line two")]],
        ["Done."],
    ])
    evs = await run_turn(ctx, "edit the readme", answer=True)
    confirms = [e for e in evs if e.type == "confirm"]
    assert confirms and confirms[0].method == "spoken_yesno" and confirms[0].tier == 2
    assert target.read_text() == "line two\n"


async def test_tier3_outside_write_denied_not_run(home):
    proj = home / "proj"
    proj.mkdir()
    dest = home / "outside" / "x.txt"
    ctx = make_ctx(proj, [
        [[_spec("write_file", path=str(dest), content="should not exist")]],
        ["Okay."],
    ])
    evs = await run_turn(ctx, "write outside", answer=False)
    confirms = [e for e in evs if e.type == "confirm"]
    assert confirms and confirms[0].method == "keyboard" and confirms[0].tier == 3
    assert not dest.exists()


async def test_vpn_warning_only_when_networked(home):
    proj = home / "proj"
    proj.mkdir()
    # bash is gated at Tier 3; deny so nothing runs.
    ctx = make_ctx(proj, [[[_spec("bash", command="vpn-full")]], ["ok"]])
    evs = await run_turn(ctx, "toggle vpn", answer=False)
    confirms = [e for e in evs if e.type == "confirm"]
    assert confirms and "network" in confirms[0].reason.lower()

    ctx2 = make_ctx(proj, [[[_spec("bash", command="vpn-full")]], ["ok"]])
    ctx2.local_mode = True
    evs2 = await run_turn(ctx2, "toggle vpn", answer=False)
    c2 = [e for e in evs2 if e.type == "confirm"]
    assert c2 and "network" not in c2[0].reason.lower()


async def test_code_only_response_not_spoken(home):
    proj = home / "proj"
    proj.mkdir()
    ctx = make_ctx(proj, [["```python\nprint('secret')\n```"]])
    evs = await run_turn(ctx, "show me the code")
    assert not any(e.type == "token" for e in evs)            # code never spoken
    assert any(e.type == "status" for e in evs)               # but a "drafted code" status


async def test_escalation_emits_status(home):
    proj = home / "proj"
    proj.mkdir()
    ctx = make_ctx(proj, [["All set."]])
    ctx.active_model = ctx.config.router.complex_model        # pretend router escalated
    evs = await run_turn(ctx, "do something complex")
    assert any(e.type == "status" for e in evs)


async def test_cancel_ends_turn(home):
    proj = home / "proj"
    proj.mkdir()
    target = proj / "readme.txt"
    target.write_text("keep me\n")
    ctx = make_ctx(proj, [
        [[_spec("edit", path=str(target), old_string="keep me", new_string="changed")]],
        ["after"],
    ])
    evs = []
    async for ev in voice_turn(ctx, "edit it"):
        evs.append(ev)
        if ev.type == "confirm":
            ctx.voice_session.clear_pending(approved=False)
            t = ctx.voice_session.active_task
            if t is not None:
                t.cancel()
    assert any(e.type == "done" for e in evs)
    assert target.read_text() == "keep me\n"                  # edit never applied
