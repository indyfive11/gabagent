import json
import types
import httpx
import pytest
import respx

from gabagent.api.client import GabAIClient
from gabagent.config.models import GabAgentConfig
from gabagent.local.ollama import unload_local


def _rl():
    return types.SimpleNamespace(record=lambda *a, **k: None)


def test_keep_alive_extra_body():
    c = GabAIClient("k", "http://localhost:11434/v1", "devstral:24b", _rl(), keep_alive="1m")
    assert c._extra_body() == {"keep_alive": "1m"}


def test_no_keep_alive_by_default():
    c = GabAIClient("k", "https://gab.ai/v1", "arya", _rl())
    assert c._extra_body() is None


@respx.mock
async def test_unload_local_posts_keep_alive_zero():
    route = respx.post("http://localhost:11434/api/generate").mock(
        return_value=httpx.Response(200, json={})
    )
    ctx = types.SimpleNamespace(
        config=GabAgentConfig(
            api_key="t", local_model="devstral:24b", local_base_url="http://localhost:11434/v1"
        )
    )
    await unload_local(ctx)
    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body == {"model": "devstral:24b", "keep_alive": 0}


async def test_unload_local_noop_without_model():
    ctx = types.SimpleNamespace(config=GabAgentConfig(api_key="t"))  # local_model == ""
    await unload_local(ctx)  # must not raise / not call out


async def test_unload_local_swallows_errors():
    # No server listening at this port -> connection error swallowed.
    ctx = types.SimpleNamespace(
        config=GabAgentConfig(
            api_key="t", local_model="devstral:24b", local_base_url="http://127.0.0.1:1/v1"
        )
    )
    await unload_local(ctx)  # best-effort, no raise


# --- malformed tool-call repair (local Ollama models) ---------------------

class _Msg:
    def __init__(self, content): self.content = content; self.tool_calls = None
class _Resp:
    def __init__(self, content): self.choices = [types.SimpleNamespace(message=_Msg(content))]
class _ParseErr(Exception):
    status_code = 400
class _Completions:
    def __init__(self): self.calls = []
    async def create(self, **kw):
        self.calls.append(kw)
        if "tools" in kw:   # first attempt (with tools) → model emits unparseable tool calls
            raise _ParseErr("The model returned tool_calls that could not be parsed "
                            "or did not match the supplied tools.")
        return _Resp("Here is a plain text answer.")
class _Client:
    def __init__(self): self.chat = types.SimpleNamespace(completions=_Completions())


async def test_malformed_toolcalls_nudge_then_without_tools():
    from gabagent.api.client import GabAIClient, _TOOLCALL_NUDGE
    from gabagent.api.models import ChatMessage
    c = GabAIClient("k", "http://localhost:11434/v1", "devstral:24b", _rl())
    c._client = _Client()
    tools = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
    out = []
    async for chunk in c.stream_complete([ChatMessage(role="user", content="hi")], tools, stream=False):
        out.append(chunk)
    # call0 (tools) parse-errors → nudge-retry WITH tools (call1) parse-errors → drop tools (call2) → text.
    assert any(isinstance(x, str) and "plain text answer" in x for x in out)
    calls = c._client.chat.completions.calls
    assert len(calls) == 3
    assert "tools" in calls[0] and "tools" in calls[1] and "tools" not in calls[2]
    assert _TOOLCALL_NUDGE in calls[1]["messages"][-1]["content"]   # the nudge was injected
    assert c.tools_supported is True   # transient — tools NOT disabled permanently


# --- transient "failed to generate" retry (streaming / premium path) ------

class _Stream:
    def __init__(self, content): self._content = content
    def __aiter__(self):
        async def _gen():
            yield types.SimpleNamespace(choices=[types.SimpleNamespace(
                delta=types.SimpleNamespace(content=self._content, tool_calls=None),
                finish_reason="stop")])
        return _gen()


class _ToolResp:
    """A non-streaming response carrying a single run_command tool call."""
    def __init__(self):
        tc = types.SimpleNamespace(id="t1", function=types.SimpleNamespace(
            name="run_command", arguments='{"command_id":"jellyfin.search"}'))
        self.choices = [types.SimpleNamespace(message=types.SimpleNamespace(content=None, tool_calls=[tc]))]


class _EscalateCompletions:
    """arya fumbles the tool call; the stronger retry_model gets it right."""
    def __init__(self): self.calls = []
    async def create(self, **kw):
        self.calls.append(kw)
        if (kw.get("model") or "arya") == "arya" and "tools" in kw:
            raise _ParseErr("api_error invalid_tool_calls: unknown_tool 'jellyfin.search'")
        return _ToolResp()


class _FallbackCompletions:
    """The escalated model keeps failing to generate; the simple model succeeds."""
    def __init__(self): self.calls = []
    async def create(self, **kw):
        self.calls.append(kw.get("model"))
        if kw.get("model") == "claude-sonnet-4-5":
            raise RuntimeError("APIError: The model failed to generate a response. code: 'inference_failed'")
        return _Stream("Recovered on arya.")


async def test_escalated_inference_failure_falls_back_to_simple_model():
    from gabagent.api.models import ChatMessage
    c = GabAIClient("k", "https://gab.ai/v1", "arya", _rl())
    comp = _FallbackCompletions()
    c._client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=comp))
    out = [x async for x in c.stream_complete([ChatMessage(role="user", content="hi")], None,
                                              model="claude-sonnet-4-5", stream=True,
                                              fallback_model="arya") if isinstance(x, str)]
    assert "".join(out) == "Recovered on arya."
    # sonnet (fail) → sonnet retry (fail) → final attempt falls back to arya (succeeds)
    assert comp.calls == ["claude-sonnet-4-5", "claude-sonnet-4-5", "arya"]


async def test_parse_error_retries_on_stronger_model_with_nudge():
    from gabagent.api.client import GabAIClient, _TOOLCALL_NUDGE
    from gabagent.api.models import ChatMessage
    c = GabAIClient("k", "https://gab.ai/v1", "arya", _rl())
    comp = _EscalateCompletions()
    c._client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=comp))
    tools = [{"type": "function", "function": {"name": "run_command", "parameters": {}}}]
    specs = None
    async for chunk in c.stream_complete([ChatMessage(role="user", content="play movie")],
                                         tools, model="arya", stream=False,
                                         retry_model="claude-sonnet-4-5"):
        if isinstance(chunk, list):
            specs = chunk
    assert specs and specs[0].name == "run_command"        # recovered a valid tool call
    assert len(comp.calls) == 2
    assert comp.calls[0].get("model") == "arya"
    assert comp.calls[1].get("model") == "claude-sonnet-4-5"           # escalated for the retry
    assert _TOOLCALL_NUDGE in comp.calls[1]["messages"][-1]["content"]  # nudged


class _ParseErrStream:
    """create() succeeds but ITERATING raises a parse error — gab.ai validates tool_calls mid-stream, so
    the open-time nudge_retry never sees it; the outer handler must recover it."""
    def __aiter__(self):
        async def _gen():
            raise _ParseErr("api_error invalid_tool_calls: unknown_tool 'tidal.set_volume'")
            yield  # unreachable — makes this an async generator
        return _gen()


class _NudgedToolStream:
    def __aiter__(self):
        async def _gen():
            yield types.SimpleNamespace(choices=[types.SimpleNamespace(
                delta=types.SimpleNamespace(content=None, tool_calls=[types.SimpleNamespace(
                    index=0, id="t1", function=types.SimpleNamespace(
                        name="run_command",
                        arguments='{"command_id":"tidal.set_volume","args":{"level":30}}'))]),
                finish_reason="tool_calls")])
        return _gen()


class _MidStreamParseCompletions:
    """arya's stream parse-errors DURING iteration; the nudged+escalated retry yields a valid call."""
    def __init__(self): self.calls = []
    async def create(self, **kw):
        self.calls.append(kw)
        msgs = kw.get("messages", [])
        nudged = bool(msgs) and msgs[-1].get("role") == "system"   # the run_command nudge was appended
        return _NudgedToolStream() if nudged else _ParseErrStream()


async def test_midstream_parse_error_self_heals_via_nudge():
    # The tidal.set_volume live miss: gab.ai rejected a command-id-called-as-a-function DURING streaming,
    # which hit the outer handler that only recovered transient errors → the turn errored. Now the outer
    # handler applies the run_command nudge + escalation for a mid-stream parse error too.
    from gabagent.api.client import GabAIClient, _TOOLCALL_NUDGE
    from gabagent.api.models import ChatMessage
    c = GabAIClient("k", "https://gab.ai/v1", "arya", _rl())
    comp = _MidStreamParseCompletions()
    c._client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=comp))
    tools = [{"type": "function", "function": {"name": "run_command", "parameters": {}}}]
    specs = None
    async for chunk in c.stream_complete([ChatMessage(role="user", content="set the music to 30")],
                                         tools, model="arya", stream=True,
                                         retry_model="claude-sonnet-4-5"):
        if isinstance(chunk, list):
            specs = chunk
    assert specs and specs[0].name == "run_command"            # recovered instead of erroring the turn
    assert len(comp.calls) == 2
    assert comp.calls[0].get("model") == "arya"
    assert comp.calls[1].get("model") == "claude-sonnet-4-5"   # escalated on the mid-stream recovery
    assert comp.calls[1]["messages"][-1]["content"] == _TOOLCALL_NUDGE
    assert c.tools_supported is True                           # not permanently disabled


class _StreamCompletions:
    def __init__(self): self.calls = 0
    async def create(self, **kw):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("The model failed to generate a response. Please try again.")
        return _Stream("Recovered answer.")


async def test_transient_generation_error_retries_once():
    from gabagent.api.client import GabAIClient
    from gabagent.api.models import ChatMessage
    c = GabAIClient("k", "https://gab.ai/v1", "arya", _rl())
    c._client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=_StreamCompletions()))
    out = []
    async for chunk in c.stream_complete([ChatMessage(role="user", content="hi")], None, stream=True):
        if isinstance(chunk, str):
            out.append(chunk)
    assert "".join(out) == "Recovered answer."
    assert c._client.chat.completions.calls == 2   # failed once, retried, succeeded


# --- transient raised DURING stream iteration (the real gap: error fires inside `async for`) -----

def _chunk(text):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(
        delta=types.SimpleNamespace(content=text, tool_calls=None), finish_reason=None)])


class _RaisingStream:
    """Opens fine, then raises the transient before yielding anything."""
    def __aiter__(self):
        async def _gen():
            raise RuntimeError("The model failed to generate a response. Please try again.")
            yield  # pragma: no cover - makes this a generator
        return _gen()


class _MidIterCompletions:
    def __init__(self): self.calls = 0
    async def create(self, **kw):
        self.calls += 1
        return _RaisingStream() if self.calls == 1 else _Stream("Recovered answer.")


async def test_transient_during_iteration_retries():
    from gabagent.api.models import ChatMessage
    c = GabAIClient("k", "https://gab.ai/v1", "arya", _rl())
    comp = _MidIterCompletions()
    c._client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=comp))
    out = [x async for x in c.stream_complete([ChatMessage(role="user", content="hi")], None, stream=True)
           if isinstance(x, str)]
    assert "".join(out) == "Recovered answer."
    assert comp.calls == 2   # the mid-iteration failure is now retried (was the bug: it wasn't)


class _PartialThenFailStream:
    """Emits a token, THEN fails — replaying would double-emit, so we must NOT retry."""
    def __aiter__(self):
        async def _gen():
            yield _chunk("Partial ")
            raise RuntimeError("The model failed to generate a response. Please try again.")
        return _gen()


class _PartialCompletions:
    def __init__(self): self.calls = 0
    async def create(self, **kw):
        self.calls += 1
        return _PartialThenFailStream()


async def test_transient_after_emission_does_not_retry():
    from gabagent.api.models import ChatMessage
    c = GabAIClient("k", "https://gab.ai/v1", "arya", _rl())
    comp = _PartialCompletions()
    c._client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=comp))
    out = []
    with pytest.raises(Exception):
        async for chunk in c.stream_complete([ChatMessage(role="user", content="hi")], None, stream=True):
            if isinstance(chunk, str):
                out.append(chunk)
    assert out == ["Partial "]   # the partial reached the caller...
    assert comp.calls == 1       # ...so we did NOT replay it (no double-speak)
