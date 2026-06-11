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


def _wire(monkeypatch, *, hint="wayne's world", screens=_SCREENS, ok=True):
    seen = {"js": None}
    async def _outputs(): return list(screens)
    async def _hint(_ctx): return hint
    async def _run(js):
        seen["js"] = js
        return ok
    monkeypatch.setattr(dk, "_kscreen_outputs", _outputs)
    monkeypatch.setattr(dk, "_movie_window_hint", _hint)
    monkeypatch.setattr(dk, "_run_kwin_script", _run)
    return seen


async def test_to_movie_screen_moves_to_configured_connector(monkeypatch):
    seen = _wire(monkeypatch)
    assert await dk.to_movie_screen(_ctx("DP-1")) == "DP-1"
    assert '"DP-1"' in seen["js"]            # the move script targets the configured output
    assert "wayne's world" in seen["js"]     # ...by the movie window's caption


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
