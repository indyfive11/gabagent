import types
import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.commands.providers import desktop as dk
from gabagent.voice import debuglog

pytestmark = pytest.mark.asyncio


def _capture_dlog(monkeypatch):
    """Capture the `movie_screen` debug events to_movie_screen emits (it imports dlog locally, so we
    patch it at the source module)."""
    events = []
    monkeypatch.setattr(debuglog, "dlog", lambda ctx, ev, **f: events.append((ev, f)))
    return events

_SCREENS = [
    {"name": "DP-1", "width": 3072, "height": 1728, "primary": True},
    {"name": "HDMI-A-1", "width": 1920, "height": 1080, "primary": False},
]


def _ctx(movie_screen="DP-1", aliases=None):
    cfg = GabAgentConfig(api_key="test")
    cfg.desktop.movie_screen = movie_screen
    if aliases:
        cfg.desktop.screen_aliases = aliases
    return types.SimpleNamespace(config=cfg)


def _wire(monkeypatch, *, hint="wayne's world", screens=_SCREENS, ok=True, landed="DP-1"):
    """Wire the KWin/display seams. `landed` is what the best-effort readback reports the window's actual
    output is ("" = readback unavailable); the move JS itself is captured in seen["js"] / seen["runs"]."""
    seen = {"js": None, "runs": 0}
    async def _outputs(): return list(screens)
    async def _hint(_ctx): return hint
    async def _run(js):
        seen["js"] = js
        seen["runs"] += 1
        return ok
    async def _out(_hint): return landed
    monkeypatch.setattr(dk, "_kscreen_outputs", _outputs)
    monkeypatch.setattr(dk, "_movie_window_hint", _hint)
    monkeypatch.setattr(dk, "_run_kwin_script", _run)
    monkeypatch.setattr(dk, "_movie_window_output", _out)
    return seen


async def test_to_movie_screen_moves_to_configured_connector(monkeypatch):
    seen = _wire(monkeypatch)
    assert await dk.to_movie_screen(_ctx("DP-1")) == "DP-1"
    assert '"DP-1"' in seen["js"]            # the move script targets the configured output
    assert "wayne's world" in seen["js"]     # ...by the movie window's caption
    assert "fullScreen=true" in seen["js"]   # ...and fullscreens it there atomically (move-last fix)


async def test_to_movie_screen_retries_when_it_lands_elsewhere(monkeypatch):
    # Readback says the window raced onto a DIFFERENT output → move is re-asserted once, reason is honest.
    events = _capture_dlog(monkeypatch)
    seen = _wire(monkeypatch, landed="HDMI-A-1")
    assert await dk.to_movie_screen(_ctx("DP-1")) == "DP-1"
    assert seen["runs"] == 2                  # one move + one retry
    assert events[-1][1]["reason"] == "moved_elsewhere" and events[-1][1]["landed"] == "HDMI-A-1"


async def test_to_movie_screen_landed_confirmed_does_not_retry(monkeypatch):
    seen = _wire(monkeypatch, landed="DP-1")
    assert await dk.to_movie_screen(_ctx("DP-1")) == "DP-1"
    assert seen["runs"] == 1                   # confirmed on the first move → no retry


async def test_to_movie_screen_unconfirmed_readback_is_best_effort(monkeypatch):
    # journald readback unavailable ("" ) → no retry, honest "moved_unconfirmed", still returns the target.
    events = _capture_dlog(monkeypatch)
    seen = _wire(monkeypatch, landed="")
    assert await dk.to_movie_screen(_ctx("DP-1")) == "DP-1"
    assert seen["runs"] == 1
    assert events[-1][1]["reason"] == "moved_unconfirmed"


async def test_to_movie_screen_resolves_alias(monkeypatch):
    seen = _wire(monkeypatch)
    ctx = _ctx("living room", aliases={"living room": "DP-1"})
    assert await dk.to_movie_screen(ctx) == "DP-1"


async def test_to_movie_screen_largest_keyword(monkeypatch):
    _wire(monkeypatch)
    assert await dk.to_movie_screen(_ctx("largest")) == "DP-1"   # biggest output


async def test_to_movie_screen_blank_setting_is_noop(monkeypatch):
    seen = _wire(monkeypatch)
    assert await dk.to_movie_screen(_ctx("")) == ""
    assert seen["js"] is None                # never invoked KWin


async def test_to_movie_screen_unconnected_screen_degrades_quietly(monkeypatch):
    # Configured screen isn't currently attached → no move, no error (auto-fullscreen just skips it).
    seen = _wire(monkeypatch)
    assert await dk.to_movie_screen(_ctx("DP-9")) == ""
    assert seen["js"] is None


async def test_to_movie_screen_no_movie_window(monkeypatch):
    seen = _wire(monkeypatch, hint="")       # nothing playing → nothing to move
    assert await dk.to_movie_screen(_ctx("DP-1")) == ""
    assert seen["js"] is None


async def test_to_movie_screen_logs_reason_on_success(monkeypatch):
    events = _capture_dlog(monkeypatch)
    _wire(monkeypatch)
    await dk.to_movie_screen(_ctx("DP-1"))
    ev, f = events[-1]
    assert ev == "movie_screen"
    assert f["reason"] == "moved" and f["target"] == "DP-1" and f["window"] == "wayne's world"


@pytest.mark.parametrize("movie_screen,hint,reason", [
    ("", "wayne's world", "blank_setting"),          # setting off → never resolved a screen
    ("DP-9", "wayne's world", "screen_not_connected"),  # configured output isn't attached
    ("DP-1", "", "no_movie_window"),                  # nothing playing to move
])
async def test_to_movie_screen_logs_reason_on_skip(monkeypatch, movie_screen, hint, reason):
    events = _capture_dlog(monkeypatch)
    _wire(monkeypatch, hint=hint)
    assert await dk.to_movie_screen(_ctx(movie_screen)) == ""
    assert events[-1] == ("movie_screen", events[-1][1])
    assert events[-1][1]["reason"] == reason
    assert events[-1][1]["moved"] == ""


# -- window-targeting matchers are scoped to a BROWSER window (the Conky/wrong-window fix) ----------

@pytest.mark.parametrize("js", [dk._JS_MOVE_NAMED, dk._JS_MOVE_FS_NAMED, dk._JS_WINDOW_OUTPUT_NAMED,
                                dk._JS_FULLSCREEN_NAMED, dk._JS_UNFULLSCREEN_NAMED])
async def test_movie_matchers_are_browser_scoped(js):
    # Every movie-window matcher gates on the browser-class predicate, so a Conky/desktop window whose
    # caption carries the hostname can never be the one moved/fullscreened.
    assert "_isB(" in js and "resourceClass" in js
    assert "chrom" in js                       # at least Chromium/Chrome is recognised as a browser
    # the old loose "class contains the hint" branch is gone — class is only consulted via _isB
    assert "k.indexOf(n)" not in js


# -- _movie_window_hint prefers the known title and self-names the owned window --------------------

class _FakePage:
    def __init__(self, title="server-name", closed=False):
        self._title, self._closed, self.stamped = title, closed, None
    def is_closed(self): return self._closed
    async def title(self): return self._title
    async def evaluate(self, fn, arg=None): self.stamped = arg


def _hint_ctx(playing_title=None, page=None):
    return types.SimpleNamespace(jellyfin_playing_title=playing_title, jellyfin_playing_page=page)


async def test_hint_prefers_known_title_and_stamps_owned_page():
    page = _FakePage(title="EndeavorMain")   # Jellyfin's lagging server-name title — must NOT win
    ctx = _hint_ctx(playing_title="The Martian (2015)", page=page)
    assert await dk._movie_window_hint(ctx) == "the martian"   # known title, cleaned
    assert page.stamped == "the martian"                       # owned window self-named to match


async def test_hint_falls_back_to_live_title_when_no_known_title():
    page = _FakePage(title="The Matrix [1080p]")
    ctx = _hint_ctx(playing_title="", page=page)
    assert await dk._movie_window_hint(ctx) == "the matrix"
    assert page.stamped is None                                # nothing to stamp without a known title


async def test_hint_uses_stored_title_when_page_is_gone():
    ctx = _hint_ctx(playing_title="Dune (2021)", page=_FakePage(closed=True))
    assert await dk._movie_window_hint(ctx) == "dune"


async def test_hint_empty_when_nothing_playing():
    assert await dk._movie_window_hint(_hint_ctx()) == ""
