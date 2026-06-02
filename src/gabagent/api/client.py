from __future__ import annotations
import json
import re
import uuid as _uuid_mod
from collections import defaultdict
from typing import AsyncIterator
from openai import AsyncOpenAI
from .models import ChatMessage, ToolCallSpec
from .rate_limit import UsageTracker

# Matches ```json ... ``` or ``` ... ``` blocks containing a JSON tool call object
_TC_BLOCK = re.compile(
    r"```(?:json)?\s*\n?(\{.*?\})\s*\n?```",
    re.DOTALL,
)


def _is_toolcall_parse_error(e: Exception) -> bool:
    """A 400 from the server because the model emitted tool calls it couldn't parse / that
    didn't match the supplied tools (seen with local Ollama models). Transient → retry once."""
    if getattr(e, "status_code", None) != 400:
        return False
    s = str(e).lower()
    return ("could not be parsed" in s
            or "did not match the supplied tools" in s
            or ("tool_calls" in s and "parse" in s))


def _is_transient_generation_error(e: Exception) -> bool:
    """An upstream hiccup where the model simply didn't produce a response (e.g. gab.ai's
    'The model failed to generate a response. Please try again.'). Not our request's fault →
    retry the same request once."""
    s = str(e).lower()
    return "failed to generate a response" in s or "please try again" in s


def _extract_text_tool_calls(content: str) -> tuple[str, list[ToolCallSpec]]:
    """Split model text that embeds tool calls as JSON into prose and ToolCallSpecs.

    Handles local models (e.g. qwen2.5-coder via Ollama) that emit tool calls
    as plain JSON text rather than structured delta.tool_calls.
    """
    specs: list[ToolCallSpec] = []

    def _try_parse(raw: str) -> ToolCallSpec | None:
        try:
            obj = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(obj, dict) or "name" not in obj or "arguments" not in obj:
            return None
        args = obj["arguments"]
        return ToolCallSpec(
            id=f"local-{_uuid_mod.uuid4().hex[:8]}",
            name=obj["name"],
            arguments=json.dumps(args) if isinstance(args, dict) else str(args),
        )

    # Case 1: entire content is a bare JSON tool call
    spec = _try_parse(content)
    if spec:
        return "", [spec]

    # Case 2: prose with one or more ```json { ... } ``` blocks
    text_parts: list[str] = []
    last = 0
    for m in _TC_BLOCK.finditer(content):
        text_parts.append(content[last : m.start()])
        spec = _try_parse(m.group(1))
        if spec:
            specs.append(spec)
        else:
            text_parts.append(m.group(0))  # not a tool call — keep as text
        last = m.end()
    text_parts.append(content[last:])
    prose = "".join(text_parts).strip()

    return prose, specs


class GabAIClient:
    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        rate_limiter: UsageTracker,
        keep_alive: str | None = None,
    ):
        self.model = model
        self.rate_limiter = rate_limiter
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self.tools_supported: bool = True  # set False if model rejects tool schemas
        # Ollama-only: per-request VRAM keep-alive (e.g. "1m"). Sent via extra_body so it
        # scopes to gabagent's requests without touching the global OLLAMA_KEEP_ALIVE default.
        self.keep_alive = keep_alive

    def _extra_body(self) -> dict | None:
        return {"keep_alive": self.keep_alive} if self.keep_alive else None

    async def stream_complete(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        model: str | None = None,
        stream: bool = True,
    ) -> AsyncIterator[str | list[ToolCallSpec]]:
        active_model = model or self.model
        self.rate_limiter.record(active_model)

        raw_messages = [m.to_dict() for m in messages]

        if not stream:
            # Non-streaming path: used for local models where streaming tool calls
            # are unreliable (Ollama returns them as text instead of delta.tool_calls).
            kwargs: dict = {
                "model": active_model,
                "messages": raw_messages,
                "stream": False,
            }
            if self.keep_alive:
                kwargs["extra_body"] = self._extra_body()
            want_tools = tools and self.tools_supported
            if want_tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            try:
                response = await self._client.chat.completions.create(**kwargs)
            except Exception as e:
                # If the model rejects tool schemas, remember and retry without them.
                if want_tools and getattr(e, "status_code", None) == 400 and "does not support tools" in str(e):
                    self.tools_supported = False
                    kwargs.pop("tools", None)
                    kwargs.pop("tool_choice", None)
                    response = await self._client.chat.completions.create(**kwargs)
                elif want_tools and _is_toolcall_parse_error(e):
                    # Local models (qwen/devstral via Ollama) sometimes emit malformed tool calls.
                    # Retry this one turn WITHOUT tools so it yields a usable text answer instead of
                    # erroring — transient, so don't disable tools permanently.
                    kwargs.pop("tools", None)
                    kwargs.pop("tool_choice", None)
                    response = await self._client.chat.completions.create(**kwargs)
                elif _is_transient_generation_error(e):
                    # Upstream didn't generate anything — retry the same request once.
                    response = await self._client.chat.completions.create(**kwargs)
                else:
                    body = getattr(e, "body", None)
                    if body:
                        raise RuntimeError(f"{type(e).__name__}: {e} | body={body}") from e
                    raise
            msg = response.choices[0].message
            if msg.tool_calls:
                # Properly structured tool calls (model supports OpenAI tool format)
                if msg.content:
                    yield msg.content
                specs = [
                    ToolCallSpec(
                        id=tc.id or f"local-{_uuid_mod.uuid4().hex[:8]}",
                        name=tc.function.name,
                        arguments=tc.function.arguments or "{}",
                    )
                    for tc in msg.tool_calls
                ]
                yield specs
            elif msg.content:
                # Model embedded tool calls as JSON text (qwen2.5-coder / Ollama pattern)
                prose, specs = _extract_text_tool_calls(msg.content)
                if prose:
                    yield prose
                if specs:
                    yield specs
                elif not prose:
                    # Unparseable — yield raw content so the user sees something
                    yield msg.content
            return

        kwargs = {
            "model": active_model,
            "messages": raw_messages,
            "stream": True,
        }
        if self.keep_alive:
            kwargs["extra_body"] = self._extra_body()
        if tools and self.tools_supported:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        tc_names: dict[int, str] = defaultdict(str)
        tc_ids: dict[int, str] = defaultdict(str)
        tc_args: dict[int, str] = defaultdict(str)
        text_buf = ""

        try:
            stream_obj = await self._client.chat.completions.create(**kwargs)
        except Exception as e:
            # If the model rejects tool schemas, retry without them.
            if tools and self.tools_supported and getattr(e, "status_code", None) == 400 and "does not support tools" in str(e):
                self.tools_supported = False
                kwargs.pop("tools", None)
                kwargs.pop("tool_choice", None)
                stream_obj = await self._client.chat.completions.create(**kwargs)
            elif tools and self.tools_supported and _is_toolcall_parse_error(e):
                # Malformed tool calls this turn → retry once without tools (transient).
                kwargs.pop("tools", None)
                kwargs.pop("tool_choice", None)
                stream_obj = await self._client.chat.completions.create(**kwargs)
            elif _is_transient_generation_error(e):
                # Upstream didn't generate anything — retry the same request once.
                stream_obj = await self._client.chat.completions.create(**kwargs)
            else:
                body = getattr(e, "body", None)
                if body:
                    raise RuntimeError(f"{type(e).__name__}: {e} | body={body}") from e
                raise
        async for chunk in stream_obj:
            choice = chunk.choices[0] if chunk.choices else None
            if choice is None:
                continue
            delta = choice.delta

            if delta.content:
                text_buf += delta.content
                yield delta.content

            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if tc_delta.id:
                        tc_ids[idx] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tc_names[idx] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            tc_args[idx] += tc_delta.function.arguments

            if choice.finish_reason in ("tool_calls", "stop") and tc_names:
                specs = [
                    ToolCallSpec(
                        id=tc_ids[i],
                        name=tc_names[i],
                        arguments=tc_args[i],
                    )
                    for i in sorted(tc_names.keys())
                ]
                yield specs
                tc_names.clear()
                tc_ids.clear()
                tc_args.clear()

    async def complete_simple(self, messages: list[ChatMessage], model: str | None = None) -> str:
        active_model = model or self.model
        self.rate_limiter.record(active_model)
        raw_messages = [m.to_dict() for m in messages]
        response = await self._client.chat.completions.create(
            model=active_model,
            messages=raw_messages,
            stream=False,
            extra_body=self._extra_body(),
        )
        return response.choices[0].message.content or ""
