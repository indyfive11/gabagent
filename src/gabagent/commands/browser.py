"""A persistent, headed Playwright browser the agent owns — the "play on screen" surface.

Idempotent (reuse the open context, like the /voice launcher); persistent user-data-dir per
profile so a one-time login survives across calls; torn down on voice-server shutdown.
"""
from __future__ import annotations
from typing import TYPE_CHECKING

from gabagent.config.paths import config_dir

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


async def _context_alive(bctx) -> bool:
    """True if the persistent context is still usable.

    A real protocol round-trip (`cookies()`) raises on a dead context — unlike the cached `.pages`
    property, which returns stale data on a CLOSED context and so reported a false "alive". That false
    positive is exactly what stranded us on a dead browser: the closed context passed the check, got
    reused, then `new_page()`/`goto()` raised "browser context is closed", and the stale ref was never
    cleared — so every retry hit the same corpse ("still getting the same error—still closed")."""
    try:
        await bctx.cookies()
        return True
    except Exception:
        return False


async def ensure_browser(ctx: AgentContext, profile: str = "default"):
    """Return a live persistent BrowserContext, launching a headed one if needed.

    Self-heals a dead browser: if the held context fails the liveness probe (window closed, browser
    crashed, shutdown race), tear it down cleanly and relaunch — so the FIRST play after a death
    recovers, with no error and no manual retry."""
    existing = ctx.persistent_browser
    if existing is not None:
        if await _context_alive(existing):
            return existing
        await close_browser(ctx)   # dead — stop its playwright + clear refs before relaunching

    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError as e:
        if e.name != "playwright":
            raise
        raise RuntimeError(
            "The browser command needs the 'playwright' package, which isn't installed. "
            "Install it:  pacman -S python-playwright   (or:  pip install playwright)"
        ) from e

    pw = await async_playwright().start()
    profile_dir = config_dir() / "browser" / profile
    profile_dir.mkdir(parents=True, exist_ok=True)
    bctx = await pw.chromium.launch_persistent_context(
        str(profile_dir), headless=False, no_viewport=True, args=["--start-maximized"]
    )
    # Proactively drop the ref the moment the context closes (user shuts the window, browser exits) so a
    # later ensure_browser doesn't even probe a corpse — it just relaunches.
    try:
        bctx.on("close", lambda _bctx: setattr(ctx, "persistent_browser", None))
    except Exception:
        pass
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
    # A dead/closed context means its pages are dead too — drop the movie page ref so _live_jellyfin_page
    # doesn't hand a stale page to a control command.
    ctx.jellyfin_playing_page = None
