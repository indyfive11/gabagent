from __future__ import annotations
from typing import TYPE_CHECKING, AsyncIterator, Protocol, runtime_checkable

from .models import ChatMessage, ToolCallSpec

if TYPE_CHECKING:
    from gabagent.config.models import GabAgentConfig
    from .rate_limit import UsageTracker


@runtime_checkable
class LLMClient(Protocol):
    """The brain seam: every LLM consumer depends only on this surface, so the backend
    (gab.ai / Claude / local Ollama) is hot-swappable."""

    model: str
    tools_supported: bool

    def stream_complete(
        self,
        messages: list[ChatMessage],
        tools: list[dict] | None = ...,
        model: str | None = ...,
        stream: bool = ...,
        retry_model: str | None = ...,
        fallback_model: str | None = ...,
        effort: str | None = ...,
    ) -> AsyncIterator[str | list[ToolCallSpec]]: ...

    async def complete_simple(
        self,
        messages: list[ChatMessage],
        model: str | None = ...,
        effort: str | None = ...,
    ) -> str: ...


def build_client(
    cfg: GabAgentConfig,
    rate_limiter: UsageTracker,
    *,
    model: str | None = None,
    api_key: str | None = None,
) -> LLMClient:
    """Construct the primary LLM client for the configured provider.

    The Ollama local_client sites stay on GabAIClient directly (always OpenAI-compatible);
    this factory is only for the primary, provider-selectable brain.
    """
    if cfg.provider == "claude":
        from .claudette import ClaudetteClient
        rung0 = cfg.claude.ladder[0]
        return ClaudetteClient(
            api_key=api_key or cfg.claude.api_key,
            base_url=cfg.base_url,
            model=model or rung0.model,
            rate_limiter=rate_limiter,
            effort=rung0.effort,
            max_tokens=cfg.claude.max_tokens,
        )

    from .client import GabAIClient
    return GabAIClient(
        api_key=api_key or cfg.api_key or "__setup_pending__",
        base_url=cfg.base_url,
        model=model or cfg.model,
        rate_limiter=rate_limiter,
    )
