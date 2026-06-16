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


def anthropic_configured(cfg: GabAgentConfig) -> bool:
    """True when a Claude client can be built (so the ladder may include the real Anthropic rungs)."""
    import os
    return bool(cfg.claude.api_key or os.environ.get("ANTHROPIC_API_KEY"))


def build_client_for(
    backend: str,
    cfg: GabAgentConfig,
    rate_limiter: UsageTracker,
    *,
    keep_alive: str = "1m",
) -> LLMClient | None:
    """Construct the client for a specific ladder backend, independent of `cfg.provider`.

    Used by the cross-backend escalation ladder so a session can hold a gab client AND a Claude
    client AND a local client at once, each serving its rung. Returns None when the backend isn't
    configured (e.g. "claude" with no Anthropic key) so the ladder simply omits those rungs.
    """
    if backend == "claude":
        if not anthropic_configured(cfg):
            return None
        from .claudette import ClaudetteClient
        rung0 = cfg.claude.ladder[0]
        return ClaudetteClient(
            api_key=cfg.claude.api_key or None,   # None → SDK reads ANTHROPIC_API_KEY
            base_url=cfg.base_url,                 # ignored by ClaudetteClient (AsyncAnthropic default)
            model=rung0.model,
            rate_limiter=rate_limiter,
            effort=rung0.effort,
            max_tokens=cfg.claude.max_tokens,
        )

    from .client import GabAIClient
    if backend == "local":
        if not cfg.local_model:
            return None
        return GabAIClient(
            api_key="ollama",
            base_url=cfg.local_base_url,
            model=cfg.local_model,
            rate_limiter=rate_limiter,
            keep_alive=keep_alive,
        )

    # "gab" — Aria on gab.ai. Pin the client's default model to the router's simple_model (Aria),
    # NOT cfg.model (which is a Claude string when provider="claude"); gab rungs send model=None.
    return GabAIClient(
        api_key=cfg.api_key or "__setup_pending__",
        base_url=cfg.base_url,
        model=cfg.router.simple_model,
        rate_limiter=rate_limiter,
    )
