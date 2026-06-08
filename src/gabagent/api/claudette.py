from __future__ import annotations
import json
from typing import AsyncIterator
from anthropic import AsyncAnthropic
from .models import ChatMessage, ToolCallSpec
from .rate_limit import UsageTracker


# Effort and adaptive thinking 400 on Haiku 4.5 (no thinking feature, no effort param). They are
# available on Opus 4.x and Sonnet 4.6. Gate by model-id prefix so the haiku bottom rung runs plain
# and an opus/sonnet escalation turns thinking + effort on.
def _supports_effort(model: str) -> bool:
    m = model.lower()
    return m.startswith("claude-opus") or m.startswith("claude-sonnet")


def _supports_adaptive(model: str) -> bool:
    return _supports_effort(model)


class ClaudetteClient:
    """Anthropic ("claudette") backend satisfying the same interface as GabAIClient.

    The whole job is translating the OpenAI-shaped ChatMessage/tool format the agent loop speaks
    into Anthropic's content-block format, then mapping the streamed response back to the gab
    contract (yield str text deltas, then ONCE a list[ToolCallSpec]).
    """

    def __init__(
        self,
        api_key: str,
        base_url: str,
        model: str,
        rate_limiter: UsageTracker,
        keep_alive: str | None = None,
        *,
        effort: str = "high",
        max_tokens: int = 8192,
    ):
        self.model = model
        self.rate_limiter = rate_limiter
        # None → SDK reads ANTHROPIC_API_KEY from the env. base_url is accepted for ctor-signature
        # parity with GabAIClient but only forwarded when set (Anthropic defaults to its own host).
        self._client = AsyncAnthropic(api_key=api_key or None)
        self.tools_supported: bool = True
        self.keep_alive = keep_alive  # accepted for parity; not used by Anthropic
        self.effort = effort
        self.max_tokens = max_tokens

    # -- format conversion -------------------------------------------------

    @staticmethod
    def _convert_messages(messages: list[ChatMessage]) -> tuple[str, list[dict]]:
        """OpenAI-shaped ChatMessage list → (system_str, anthropic_messages).

        - role=="system" messages are pulled out and concatenated into the top-level system param.
        - assistant → a text block (if non-empty) plus tool_use blocks.
        - consecutive tool results are coalesced into ONE user message of tool_result blocks
          (Anthropic requires a parallel tool batch's results in a single user turn).
        """
        system_parts: list[str] = []
        out: list[dict] = []

        for m in messages:
            if m.role == "system":
                if m.content:
                    system_parts.append(m.content)
                continue

            if m.role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id or "",
                    "content": m.content or "",
                }
                # Coalesce into a trailing user message if the previous one is a tool_result batch.
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                        and out[-1]["content"] and out[-1]["content"][0].get("type") == "tool_result":
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
                continue

            if m.role == "user":
                out.append({"role": "user", "content": m.content or ""})
                continue

            if m.role == "assistant":
                blocks: list[dict] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in (m.tool_calls or []):
                    try:
                        tc_input = json.loads(tc.arguments) if tc.arguments else {}
                    except (json.JSONDecodeError, ValueError):
                        tc_input = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc_input,
                    })
                if blocks:  # skip a wholly-empty assistant turn
                    out.append({"role": "assistant", "content": blocks})
                continue

        paired = ClaudetteClient._pair_tool_blocks(out)
        if not paired:
            # Anthropic requires >=1 message. Conversion empties the list when the windowed history was
            # system-only, or only an orphan tool block the pairing pass culled (e.g. a tool follow-up
            # turn whose tool_use was trimmed by the recent-tail slice). Recover the most recent usable
            # content so the turn degrades gracefully instead of 400ing the whole turn.
            fallback = next(
                (m.content for m in reversed(messages) if m.role != "system" and m.content),
                "(continue)",
            )
            paired = [{"role": "user", "content": fallback}]
        return "\n\n".join(system_parts), paired

    @staticmethod
    def _pair_tool_blocks(out: list[dict]) -> list[dict]:
        """Drop tool_use/tool_result blocks that lost their partner, plus any message left empty.

        Anthropic strictly requires every tool_use to be followed by its tool_result and every
        tool_result to be preceded by its tool_use (gab.ai tolerated unpaired blocks; Anthropic
        400s). History windowing (voice/turn.py, agent/loop.py slice the recent tail) can cut
        between a pair, and an aborted/barge-in turn can leave a tool_use with no result — both
        desync the list. Enforce the invariant here so the payload is valid regardless of how
        upstream trimmed it.
        """
        use_ids: set[str] = set()
        result_ids: set[str] = set()
        for m in out:
            content = m["content"]
            if not isinstance(content, list):
                continue
            for b in content:
                if b.get("type") == "tool_use":
                    use_ids.add(b.get("id"))
                elif b.get("type") == "tool_result":
                    result_ids.add(b.get("tool_use_id"))

        cleaned: list[dict] = []
        for m in out:
            content = m["content"]
            if not isinstance(content, list):
                cleaned.append(m)
                continue
            kept = [
                b for b in content
                if not (b.get("type") == "tool_use" and b.get("id") not in result_ids)
                and not (b.get("type") == "tool_result" and b.get("tool_use_id") not in use_ids)
            ]
            if kept:  # drop a message emptied by the cull (orphan-only result, dangling-only use)
                cleaned.append({**m, "content": kept})
        return cleaned

    @staticmethod
    def _convert_tools(tools: list[dict] | None) -> list[dict] | None:
        """OpenAI tool schemas → Anthropic tool schemas."""
        if not tools:
            return None
        converted: list[dict] = []
        for t in tools:
            fn = t.get("function", t)
            converted.append({
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return converted

    def _build_kwargs(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None,
        model: str | None,
        effort: str | None,
    ) -> tuple[dict, str]:
        active_model = model or self.model
        system, anth_messages = self._convert_messages(messages)
        kwargs: dict = {
            "model": active_model,
            "max_tokens": self.max_tokens,
            "messages": anth_messages,
        }
        if system:
            kwargs["system"] = system
        anth_tools = self._convert_tools(tools) if self.tools_supported else None
        if anth_tools:
            kwargs["tools"] = anth_tools

        eff = effort if effort is not None else self.effort
        if _supports_adaptive(active_model):
            kwargs["thinking"] = {"type": "adaptive"}
        if eff and _supports_effort(active_model):
            kwargs["output_config"] = {"effort": eff}
        return kwargs, active_model

    # -- the interface -----------------------------------------------------

    async def stream_complete(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = None,
        model: str | None = None,
        stream: bool = True,
        retry_model: str | None = None,
        fallback_model: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[str | list[ToolCallSpec]]:
        kwargs, active_model = self._build_kwargs(messages, tools, model, effort)
        self.rate_limiter.record(active_model)

        try:
            async with self._client.messages.stream(**kwargs) as stream_obj:
                async for text in stream_obj.text_stream:
                    yield text
                final = await stream_obj.get_final_message()
        except Exception as e:
            # Minimal belt: if nothing streamed yet and a fallback model is set, retry once on it.
            # (The SDK already retries transient 429/5xx internally.)
            if fallback_model and fallback_model != active_model:
                kwargs["model"] = fallback_model
                if not _supports_effort(fallback_model):
                    kwargs.pop("output_config", None)
                if not _supports_adaptive(fallback_model):
                    kwargs.pop("thinking", None)
                self.rate_limiter.record(fallback_model)
                async with self._client.messages.stream(**kwargs) as stream_obj:
                    async for text in stream_obj.text_stream:
                        yield text
                    final = await stream_obj.get_final_message()
            else:
                raise

        specs = [
            ToolCallSpec(id=b.id, name=b.name, arguments=json.dumps(b.input))
            for b in final.content
            if getattr(b, "type", None) == "tool_use"
        ]
        if specs:
            yield specs

    async def complete_simple(
        self,
        messages: list[ChatMessage],
        model: str | None = None,
        effort: str | None = None,
    ) -> str:
        kwargs, active_model = self._build_kwargs(messages, None, model, effort)
        self.rate_limiter.record(active_model)
        response = await self._client.messages.create(**kwargs)
        for block in response.content:
            if getattr(block, "type", None) == "text":
                return block.text
        return ""
