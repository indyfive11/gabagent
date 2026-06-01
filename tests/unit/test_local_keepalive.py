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
