import json
import types
import pytest

from gabagent.api.claudette import ClaudetteClient, _supports_effort, _supports_adaptive
from gabagent.api.models import ChatMessage, ToolCallSpec
from gabagent.api.rate_limit import UsageTracker


def _block(**kw):
    return types.SimpleNamespace(**kw)


class _FakeStream:
    def __init__(self, texts, final):
        self._texts = texts
        self._final = final

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def text_stream(self):
        async def _gen():
            for t in self._texts:
                yield t
        return _gen()

    async def get_final_message(self):
        return self._final


class _FakeMessages:
    def __init__(self, texts, final, create_text="hello"):
        self._texts = texts
        self._final = final
        self._create_text = create_text
        self.stream_kwargs = None
        self.create_kwargs = None

    def stream(self, **kwargs):
        self.stream_kwargs = kwargs
        return _FakeStream(self._texts, self._final)

    async def create(self, **kwargs):
        self.create_kwargs = kwargs
        return types.SimpleNamespace(content=[_block(type="text", text=self._create_text)])


def _client(messages_obj, model="claude-opus-4-8", effort="high"):
    cl = ClaudetteClient("sk-test", "https://x", model, UsageTracker(simple_model=model), effort=effort)
    cl._client = types.SimpleNamespace(messages=messages_obj)
    return cl


# -- conversion --------------------------------------------------------------

def test_system_extraction_and_tool_result_coalescing():
    msgs = [
        ChatMessage(role="system", content="A"),
        ChatMessage(role="system", content="B"),
        ChatMessage(role="user", content="hi"),
        ChatMessage(role="assistant", content="ok",
                    tool_calls=[ToolCallSpec(id="toolu_1", name="read", arguments='{"p":1}')]),
        ChatMessage(role="tool", content="r1", tool_call_id="toolu_1"),
        ChatMessage(role="tool", content="r2", tool_call_id="toolu_2"),
    ]
    system, out = ClaudetteClient._convert_messages(msgs)
    assert system == "A\n\nB"
    # user, assistant(text+tool_use), one coalesced user(tool_result x2)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    asst = out[1]["content"]
    assert asst[0] == {"type": "text", "text": "ok"}
    assert asst[1] == {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {"p": 1}}
    results = out[2]["content"]
    assert [b["type"] for b in results] == ["tool_result", "tool_result"]
    assert results[0]["tool_use_id"] == "toolu_1"
    assert results[1]["tool_use_id"] == "toolu_2"


def test_empty_assistant_turn_skipped():
    msgs = [ChatMessage(role="assistant", content=None)]
    _, out = ClaudetteClient._convert_messages(msgs)
    assert out == []


def test_tool_schema_conversion():
    tools = [{"type": "function", "function": {
        "name": "read", "description": "d", "parameters": {"type": "object", "properties": {}}}}]
    conv = ClaudetteClient._convert_tools(tools)
    assert conv == [{"name": "read", "description": "d",
                     "input_schema": {"type": "object", "properties": {}}}]
    assert ClaudetteClient._convert_tools(None) is None


# -- streaming ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_yields_text_then_toolcalls():
    final = _block(content=[
        _block(type="text", text="ignored"),
        _block(type="tool_use", id="toolu_9", name="edit", input={"a": 1}),
    ])
    fm = _FakeMessages(["he", "llo"], final)
    cl = _client(fm)
    out = []
    async for chunk in cl.stream_complete([ChatMessage(role="user", content="x")]):
        out.append(chunk)
    assert out[0] == "he"
    assert out[1] == "llo"
    assert isinstance(out[2], list)
    spec = out[2][0]
    assert isinstance(spec, ToolCallSpec)
    assert spec.id == "toolu_9" and spec.name == "edit"
    assert json.loads(spec.arguments) == {"a": 1}


@pytest.mark.asyncio
async def test_stream_no_toolcalls_yields_only_text():
    final = _block(content=[_block(type="text", text="done")])
    fm = _FakeMessages(["done"], final)
    cl = _client(fm)
    out = [c async for c in cl.stream_complete([ChatMessage(role="user", content="x")])]
    assert out == ["done"]  # no trailing list when there are no tool_use blocks


@pytest.mark.asyncio
async def test_complete_simple_returns_first_text_block():
    fm = _FakeMessages([], None, create_text="answer")
    cl = _client(fm)
    txt = await cl.complete_simple([ChatMessage(role="user", content="q")])
    assert txt == "answer"


# -- effort capability gating ------------------------------------------------

def test_effort_helpers():
    assert _supports_effort("claude-opus-4-8")
    assert _supports_effort("claude-sonnet-4-6")
    assert not _supports_effort("claude-haiku-4-5")
    assert _supports_adaptive("claude-opus-4-8")
    assert not _supports_adaptive("claude-haiku-4-5")


@pytest.mark.asyncio
async def test_opus_gets_effort_and_thinking():
    fm = _FakeMessages(["x"], _block(content=[_block(type="text", text="x")]))
    cl = _client(fm, model="claude-opus-4-8", effort="high")
    async for _ in cl.stream_complete([ChatMessage(role="user", content="x")], effort="max"):
        pass
    assert fm.stream_kwargs["output_config"] == {"effort": "max"}
    assert fm.stream_kwargs["thinking"] == {"type": "adaptive"}


@pytest.mark.asyncio
async def test_haiku_omits_effort_and_thinking():
    fm = _FakeMessages(["x"], _block(content=[_block(type="text", text="x")]))
    cl = _client(fm, model="claude-haiku-4-5", effort="")
    async for _ in cl.stream_complete([ChatMessage(role="user", content="x")], effort="high"):
        pass
    assert "output_config" not in fm.stream_kwargs
    assert "thinking" not in fm.stream_kwargs


@pytest.mark.asyncio
async def test_per_call_model_override_regates_effort():
    # Instance model is opus, but a per-call haiku override must drop effort/thinking.
    fm = _FakeMessages(["x"], _block(content=[_block(type="text", text="x")]))
    cl = _client(fm, model="claude-opus-4-8", effort="high")
    async for _ in cl.stream_complete([ChatMessage(role="user", content="x")],
                                      model="claude-haiku-4-5", effort="high"):
        pass
    assert fm.stream_kwargs["model"] == "claude-haiku-4-5"
    assert "output_config" not in fm.stream_kwargs
    assert "thinking" not in fm.stream_kwargs
