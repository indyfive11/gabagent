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
                    tool_calls=[ToolCallSpec(id="toolu_1", name="read", arguments='{"p":1}'),
                                ToolCallSpec(id="toolu_2", name="read", arguments='{"p":2}')]),
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
    # The wholly-empty assistant turn is still skipped (no assistant block), but the empty-array guard
    # then injects a placeholder user message — Anthropic requires >=1 message, so out is never empty.
    msgs = [ChatMessage(role="assistant", content=None)]
    _, out = ClaudetteClient._convert_messages(msgs)
    assert out == [{"role": "user", "content": "(continue)"}]
    assert all(m["role"] != "assistant" for m in out)


def test_orphan_tool_result_only_recovers_content():
    # A tool follow-up turn whose tool_use was trimmed by the window slice: the lone orphan tool_result
    # is culled by the pairing pass, which would empty the list. The guard recovers its content as a
    # user message so the turn degrades gracefully instead of sending messages=[] (Anthropic 400).
    msgs = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="tool", content="VOL=70", tool_call_id="toolu_x"),
    ]
    system, out = ClaudetteClient._convert_messages(msgs)
    assert system == "sys"
    assert out == [{"role": "user", "content": "VOL=70"}]


def test_system_only_input_gets_placeholder():
    # Degenerate system-only window: no usable content to recover, so the guard falls back to a neutral
    # placeholder rather than an empty (400-ing) message list.
    msgs = [ChatMessage(role="system", content="just instructions")]
    system, out = ClaudetteClient._convert_messages(msgs)
    assert system == "just instructions"
    assert out == [{"role": "user", "content": "(continue)"}]


def test_orphan_leading_tool_result_dropped():
    # History windowing trimmed off the assistant tool_use, leaving a result whose tool_use is gone.
    # Anthropic 400s if the message list starts with such an orphan; the sanitizer must drop it.
    msgs = [
        ChatMessage(role="tool", content="stale", tool_call_id="toolu_x"),
        ChatMessage(role="user", content="next thing"),
        ChatMessage(role="assistant", content="sure"),
    ]
    _, out = ClaudetteClient._convert_messages(msgs)
    assert [m["role"] for m in out] == ["user", "assistant"]
    # no tool_result block survives anywhere
    for m in out:
        if isinstance(m["content"], list):
            assert all(b.get("type") != "tool_result" for b in m["content"])


def test_dangling_tool_use_dropped_text_kept():
    # An aborted/barge-in turn left a tool_use with no following result; Anthropic 400s on it.
    msgs = [
        ChatMessage(role="user", content="do it"),
        ChatMessage(role="assistant", content="working on it",
                    tool_calls=[ToolCallSpec(id="toolu_y", name="edit", arguments="{}")]),
        ChatMessage(role="user", content="never mind"),
    ]
    _, out = ClaudetteClient._convert_messages(msgs)
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    asst = out[1]["content"]
    assert asst == [{"type": "text", "text": "working on it"}]  # tool_use dropped, text retained


def test_dangling_tool_use_only_drops_whole_message():
    # Assistant message that was *only* a dangling tool_use (no text) is removed entirely.
    msgs = [
        ChatMessage(role="user", content="do it"),
        ChatMessage(role="assistant", content=None,
                    tool_calls=[ToolCallSpec(id="toolu_z", name="edit", arguments="{}")]),
        ChatMessage(role="user", content="stop"),
    ]
    _, out = ClaudetteClient._convert_messages(msgs)
    assert [m["role"] for m in out] == ["user", "user"]


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
