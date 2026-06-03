from __future__ import annotations
import asyncio
import json
import re
import uuid as _uuid_mod
from collections import defaultdict
from typing import AsyncIterator
from openai import AsyncOpenAI
from .models import ChatMessage, ToolCallSpec
from .rate_limit import UsageTracker

# Transient "model failed to generate" hiccups: retry the same request a bounded number of times,
# with a short backoff. Streaming retries are gated on "nothing yielded yet" (see stream_complete).
# On the final attempt, an escalated turn falls back to the simpler model (fallback_model).
_MAX_GEN_ATTEMPTS = 3
_GEN_BACKOFF = 0.6

# Matches ```json ... ``` or ``` ... ``` blocks containing a JSON tool call object
_TC_BLOCK = re.compile(
    r"```(?:json)?\s*\n?(\{.*?\})\s*\n?```",
    re.DOTALL,
)


def _is_toolcall_parse_error(e: Exception) -> bool:
    """The server rejected the model's tool calls — it couldn't parse them, or the model named a tool
    that wasn't supplied (e.g. it called a catalog command-id like 'jellyfin.search' as if it were a
    function). Arrives as a 400 OR wrapped as a RuntimeError, so match on the message text, not status."""
    s = str(e).lower()
    return ("did not match the supplied tools" in s
            or "invalid_tool_calls" in s
            or "unknown_tool" in s
            or ("tool_calls" in s and "could not be parsed" in s)
            or ("tool" in s and "could not be parsed" in s))


# Appended on a parse-error retry: the model called a command-id as a function; remind it to wrap in
# run_command. Paired with escalating the retry to a stronger model (arya is unreliable at this).
_TOOLCALL_NUDGE = (
    "Only run_command, list_capabilities, and rescan_capabilities are callable tools. To use a "
    "capability such as jellyfin.search or tidal.recommendations, call run_command with command_id set "
    "to that id and the parameters in args — never call the capability name as a function or tool."
)


def _is_transient_generation_error(e: Exception) -> bool:
    """An upstream hiccup where the model simply didn't produce a response (e.g. gab.ai's
    'The model failed to generate a response. Please try again.', code 'inference_failed'). Not our
    request's fault → retry, and if the escalated model keeps failing, fall back to the simpler one."""
    s = str(e).lower()
    return ("failed to generate a response" in s or "please try again" in s
            or "inference_failed" in s)


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
        retry_model: str | None = None,
        fallback_model: str | None = None,
    ) -> AsyncIterator[str | list[ToolCallSpec]]:
        active_model = model or self.model
        self.rate_limiter.record(active_model)

        raw_messages = [m.to_dict() for m in messages]

        async def _nudge_retry(kw: dict):
            """Recover from the model calling a command-id as a function: retry WITH tools, nudging it
            to use run_command, on the stronger retry_model if given. Caller falls back to no-tools if
            this still parse-errors."""
            nudged = dict(kw)
            nudged["messages"] = raw_messages + [{"role": "system", "content": _TOOLCALL_NUDGE}]
            if retry_model and retry_model != (kw.get("model") or active_model):
                nudged["model"] = retry_model
                self.rate_limiter.record(retry_model)
            return await self._client.chat.completions.create(**nudged)

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
                    # The model emitted bad tool calls (e.g. called a command-id as a function). Retry
                    # with tools + a nudge (escalated if retry_model is set); if it STILL parse-errors,
                    # drop tools so it yields a usable text answer instead of erroring.
                    try:
                        response = await _nudge_retry(kwargs)
                    except Exception as e2:
                        if not _is_toolcall_parse_error(e2):
                            raise
                        kwargs.pop("tools", None)
                        kwargs.pop("tool_choice", None)
                        response = await self._client.chat.completions.create(**kwargs)
                elif _is_transient_generation_error(e):
                    # Upstream didn't generate anything — back off briefly and retry the same request.
                    await asyncio.sleep(_GEN_BACKOFF)
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

        # Retry the WHOLE open+iterate on a transient "failed to generate" — but only while nothing
        # has been yielded yet. Callers (agent loop / voice turn) emit text deltas immediately, so
        # replaying after partial output would double-emit; "failed to generate" yields zero tokens,
        # so the common case is safely retryable.
        nudged_for_parse = False   # have we already added the run_command nudge for a mid-stream parse error?
        for attempt in range(_MAX_GEN_ATTEMPTS):
            tc_names: dict[int, str] = defaultdict(str)
            tc_ids: dict[int, str] = defaultdict(str)
            tc_args: dict[int, str] = defaultdict(str)
            yielded_any = False
            try:
                try:
                    stream_obj = await self._client.chat.completions.create(**kwargs)
                except Exception as e:
                    # If the model rejects tool schemas, retry without them (same attempt).
                    if tools and self.tools_supported and getattr(e, "status_code", None) == 400 and "does not support tools" in str(e):
                        self.tools_supported = False
                        kwargs.pop("tools", None)
                        kwargs.pop("tool_choice", None)
                        stream_obj = await self._client.chat.completions.create(**kwargs)
                    elif tools and self.tools_supported and _is_toolcall_parse_error(e):
                        # The model called a command-id as a function → retry with tools + a nudge
                        # (escalated to retry_model). If it STILL parse-errors, drop tools for a
                        # graceful text answer.
                        try:
                            stream_obj = await _nudge_retry(kwargs)
                        except Exception as e2:
                            if not _is_toolcall_parse_error(e2):
                                raise
                            kwargs.pop("tools", None)
                            kwargs.pop("tool_choice", None)
                            stream_obj = await self._client.chat.completions.create(**kwargs)
                    else:
                        raise  # transient / other → handled by the outer except below

                async for chunk in stream_obj:
                    choice = chunk.choices[0] if chunk.choices else None
                    if choice is None:
                        continue
                    delta = choice.delta

                    if delta.content:
                        yield delta.content
                        yielded_any = True

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
                        yielded_any = True
                        tc_names.clear()
                        tc_ids.clear()
                        tc_args.clear()
                return
            except Exception as e:
                if (not yielded_any and attempt + 1 < _MAX_GEN_ATTEMPTS
                        and _is_transient_generation_error(e)):
                    await asyncio.sleep(_GEN_BACKOFF)
                    # Going into the FINAL attempt: if the escalated model keeps failing to generate,
                    # fall back to the simpler model for this turn — a simpler answer beats an error.
                    if (attempt == _MAX_GEN_ATTEMPTS - 2 and fallback_model
                            and fallback_model != (kwargs.get("model") or active_model)):
                        kwargs["model"] = fallback_model
                        self.rate_limiter.record(fallback_model)
                    continue
                # The model called a command-id as a function (e.g. tidal.set_volume) → gab.ai rejects the
                # tool_calls. With stream=True this arrives DURING iteration (not at the open create()), so
                # the open-time nudge_retry above never sees it — recover it here instead, as long as
                # nothing was emitted yet (replaying mid-output would double-emit). First retry adds the
                # run_command nudge + escalates; if it STILL parse-errors, drop tools for a graceful text
                # answer. Mirrors the open-time path so the slip self-heals on every transport.
                if (not yielded_any and attempt + 1 < _MAX_GEN_ATTEMPTS
                        and tools and self.tools_supported and "tools" in kwargs
                        and _is_toolcall_parse_error(e)):
                    if not nudged_for_parse:
                        kwargs["messages"] = raw_messages + [{"role": "system", "content": _TOOLCALL_NUDGE}]
                        if retry_model and retry_model != (kwargs.get("model") or active_model):
                            kwargs["model"] = retry_model
                            self.rate_limiter.record(retry_model)
                        nudged_for_parse = True
                    else:
                        kwargs.pop("tools", None)         # nudge didn't take → no-tools text answer
                        kwargs.pop("tool_choice", None)
                    continue
                body = getattr(e, "body", None)
                if body:
                    raise RuntimeError(f"{type(e).__name__}: {e} | body={body}") from e
                raise

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
