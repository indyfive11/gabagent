"""A persistent, headed Playwright browser the agent owns — the "play on screen" surface.

Idempotent (reuse the open context, like the /voice launcher); persistent user-data-dir per
profile so a one-time login survives across calls; torn down on voice-server shutdown.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

from gabagent.config.paths import config_dir

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


async def ensure_browser(ctx: AgentContext, profile: str = "default"):
    """Return a live persistent BrowserContext, launching a headed one if needed."""
    existing = ctx.persistent_browser
    if existing is not None:
        try:
            _ = existing.pages  # raises if the context was closed
            return existing
        except Exception:
            ctx.persistent_browser = None

    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    profile_dir = config_dir() / "browser" / profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    bctx = await pw.chromium.launch_persistent_context(
        str(profile_dir), headless=False, no_viewport=True, args=["--start-maximized"]
    )
    ctx.persistent_browser = bctx
    ctx.persistent_browser_pw = pw
    return bctx


async def close_browser(ctx: AgentContext) -> None:
    try:
        if ctx.persistent_browser is not None:
            await ctx.persistent_browser.close()
    except Exception:
        pass
    try:
        if ctx.persistent_browser_pw is not None:
            await ctx.persistent_browser_pw.stop()
    except Exception:
        pass
    ctx.persistent_browser = None
    ctx.persistent_browser_pw = None
