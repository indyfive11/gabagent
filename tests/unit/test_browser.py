"""Persistent-browser lifecycle: the self-heal that recovers from a dead context (the "browser context
is closed" crash, where the old `.pages` liveness probe returned stale data on a closed context and we
reused the corpse on every retry)."""
import types


async def test_context_alive_true_and_false():
    from gabagent.commands.browser import _context_alive
    class _Live:
        async def cookies(self): return []
    class _Dead:
        async def cookies(self): raise RuntimeError("browser context is closed")
    assert await _context_alive(_Live()) is True
    assert await _context_alive(_Dead()) is False          # a real round-trip raises on a dead context


async def test_ensure_browser_reuses_live_context():
    from gabagent.commands import browser as br
    class _Live:
        async def cookies(self): return []
    live = _Live()
    ctx = types.SimpleNamespace(persistent_browser=live, persistent_browser_pw=None,
                                jellyfin_playing_page=None)
    got = await br.ensure_browser(ctx)                     # alive → short-circuits, never relaunches
    assert got is live


async def test_ensure_browser_relaunches_dead_context(monkeypatch):
    from gabagent.commands import browser as br
    closed = {"n": 0}
    class _Dead:
        async def cookies(self): raise RuntimeError("closed")
        async def close(self): closed["n"] += 1
    sentinel = types.SimpleNamespace(on=lambda *a, **k: None)
    class _PWChromium:
        @staticmethod
        async def launch_persistent_context(*a, **k): return sentinel
    class _PW:
        chromium = _PWChromium()
        async def stop(self): ...
    class _Starter:
        async def start(self): return _PW()
    # ensure_browser imports async_playwright from playwright.async_api inside the function — patch there.
    monkeypatch.setattr("playwright.async_api.async_playwright", lambda: _Starter())

    ctx = types.SimpleNamespace(persistent_browser=_Dead(), persistent_browser_pw=None,
                                jellyfin_playing_page="stale")
    got = await br.ensure_browser(ctx)
    assert got is sentinel and ctx.persistent_browser is sentinel   # dead context → relaunched fresh
    assert closed["n"] == 1                                          # the corpse was closed
    assert ctx.jellyfin_playing_page is None                        # close_browser cleared the stale page


async def test_close_browser_clears_movie_page():
    from gabagent.commands import browser as br
    ctx = types.SimpleNamespace(persistent_browser=None, persistent_browser_pw=None,
                                jellyfin_playing_page="stale")
    await br.close_browser(ctx)
    assert ctx.jellyfin_playing_page is None
