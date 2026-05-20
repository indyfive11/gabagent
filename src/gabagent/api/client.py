from __future__ import annotations
import json
from collections import defaultdict
from typing import AsyncIterator
from openai import AsyncOpenAI
from .models import ChatMessage, ToolCallSpec
from .rate_limit import UsageTracker


class GabAIClient:
    def __init__(self, api_key: str, base_url: str, model: str, rate_limiter: UsageTracker):
        self.model = model
        self.rate_limiter = rate_limiter
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def stream_complete(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        model: str | None = None,
    ) -> AsyncIterator[str | list[ToolCallSpec]]:
        active_model = model or self.model
        self.rate_limiter.record(active_model)

        raw_messages = [m.to_dict() for m in messages]
        kwargs: dict = {
            "model": active_model,
            "messages": raw_messages,
            "stream": True,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        tc_names: dict[int, str] = defaultdict(str)
        tc_ids: dict[int, str] = defaultdict(str)
        tc_args: dict[int, str] = defaultdict(str)
        text_buf = ""

        try:
            stream = await self._client.chat.completions.create(**kwargs)
        except Exception as e:
            body = getattr(e, "body", None)
            if body:
                raise type(e)(f"{e} | body={body}") from e
            raise
        async for chunk in stream:
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
        )
        return response.choices[0].message.content or ""
