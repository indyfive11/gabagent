import json
import types
import httpx
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


async def test_malformed_toolcalls_retry_without_tools():
    from gabagent.api.client import GabAIClient
    from gabagent.api.models import ChatMessage
    c = GabAIClient("k", "http://localhost:11434/v1", "devstral:24b", _rl())
    c._client = _Client()
    tools = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
    out = []
    async for chunk in c.stream_complete([ChatMessage(role="user", content="hi")], tools, stream=False):
        out.append(chunk)
    # It retried once without tools and produced a usable text answer instead of erroring.
    assert any(isinstance(x, str) and "plain text answer" in x for x in out)
    calls = c._client.chat.completions.calls
    assert len(calls) == 2 and "tools" in calls[0] and "tools" not in calls[1]
    assert c.tools_supported is True   # transient — tools NOT disabled permanently
