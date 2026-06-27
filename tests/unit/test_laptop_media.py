"""Laptop media (Jellyfin cast + local Mopidy on a non-KDE single-room brain). Covers the three additive,
empty-default brain changes agreed with VAC: the global jellyfin.client_target fallback, the non-KDE
owned-browser desktop guard, and the cast-movie node exclusion from the brain's full-mute local-duck (the
satellite belt owns that node). All defaults keep EM/Pi byte-identical."""
from types import SimpleNamespace

import pytest

from gabagent.commands.providers import jellyfin as J
from gabagent.config.models import GabAgentConfig, JellyfinConfig, RoomMediaProfile
from gabagent.voice import ducking as _dk
from gabagent.voice.turn import _has_media_capability


def _cfg(client_target="", room_media=None, cast_excl=""):
    return GabAgentConfig(
        api_key="t",
        jellyfin=JellyfinConfig(base_url="http://em:8096", api_key="EMKEY",
                                client_target=client_target, cast_duck_exclude_match=cast_excl),
        room_media=room_media or {},
    )


def _ctx(cfg, room_id=None, **extra):
    return SimpleNamespace(config=cfg, room_id=room_id, **extra)


# -- _room_client_target: per-room → global → none --------------------------

def test_client_target_global_fallback_when_no_room_media():
    # The laptop case: no room_media at all, just the global field → cast to it.
    assert J._room_client_target(_ctx(_cfg(client_target="laptop-jellyfin"))) == "laptop-jellyfin"


def test_client_target_per_room_wins_over_global():
    rm = {"raspi": RoomMediaProfile(jellyfin_client_target="raspi-jellyfin")}
    ctx = _ctx(_cfg(client_target="laptop-jellyfin", room_media=rm), room_id="raspi")
    assert J._room_client_target(ctx) == "raspi-jellyfin"


def test_client_target_falls_to_global_when_room_profile_has_no_target():
    # A room with a profile (e.g. only a tidal override) but no jellyfin_client_target → use the global.
    rm = {"den": RoomMediaProfile(tidal_rpc_url="http://den:6680/mopidy/rpc")}
    ctx = _ctx(_cfg(client_target="laptop-jellyfin", room_media=rm), room_id="den")
    assert J._room_client_target(ctx) == "laptop-jellyfin"


def test_client_target_empty_when_neither_set_is_em_browser_path():
    # EM/default: no global, no per-room → "" → owned-browser path (byte-identical).
    assert J._room_client_target(_ctx(_cfg())) == ""


# -- media-capability honesty gate (the muzzle regression) ------------------

def test_media_capability_true_via_global_client_target_when_catalog_empty():
    # The laptop regression: catalog has no media (transient detect miss at startup) AND no room_media,
    # but the global client_target is set → the room CAN play, so the honesty gate must NOT muzzle it.
    ctx = _ctx(_cfg(client_target="laptop-jellyfin"))  # no command_catalog attr ⇒ path (a) skipped
    assert _has_media_capability(ctx) is True


def test_media_capability_false_when_no_catalog_and_no_target():
    # A genuinely media-less room (e.g. `mobile`): no catalog media, no cast target → muzzle stays.
    assert _has_media_capability(_ctx(_cfg())) is False


def test_media_capability_true_via_per_room_target():
    rm = {"raspi": RoomMediaProfile(jellyfin_client_target="raspi-jellyfin")}
    assert _has_media_capability(_ctx(_cfg(room_media=rm), room_id="raspi")) is True


def test_media_capability_true_when_catalog_has_media():
    # EM/default: no cast target, but the catalog detected media → True (unchanged).
    class _Cat:
        def domains(self):
            return ["media", "system"]
    ctx = _ctx(_cfg(), command_catalog=_Cat())
    assert _has_media_capability(ctx) is True


# -- desktop guard ----------------------------------------------------------

def test_is_kde_wayland_desktop_env_detection(monkeypatch):
    monkeypatch.delenv("KDE_FULL_SESSION", raising=False)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    assert J._is_kde_wayland_desktop() is True
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "plasma:KDE")  # substring match
    assert J._is_kde_wayland_desktop() is True
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "X-Cinnamon")
    assert J._is_kde_wayland_desktop() is False
    monkeypatch.setenv("KDE_FULL_SESSION", "true")           # secondary signal
    assert J._is_kde_wayland_desktop() is True


@pytest.mark.asyncio
async def test_play_non_kde_no_cast_target_gives_clear_error(monkeypatch):
    # Cinnamon laptop misconfigured (no cast target) must NOT fall into the cryptic KWin browser path —
    # it returns a clear, actionable error pointing at jellyfin.client_target. No network is touched
    # (target is empty, so play() never reaches the cast /Sessions call before the guard).
    monkeypatch.setattr(J, "_is_kde_wayland_desktop", lambda: False)
    res = await J.play(_ctx(_cfg()), item_id="abc", title="Some Movie")
    assert not res.success
    assert "client_target" in (res.error or "")


@pytest.mark.asyncio
async def test_play_kde_no_target_does_not_trip_the_guard(monkeypatch):
    # On KDE the guard must be transparent: with no target it proceeds to the browser path. We stub the
    # browser play so no real browser launches, and assert the guard didn't short-circuit with its error.
    monkeypatch.setattr(J, "_is_kde_wayland_desktop", lambda: True)
    called = {}

    async def _fake_browser(ctx, jc, item_id, title="", start_secs=None):
        called["hit"] = True
        from gabagent.api.models import ToolResult
        return ToolResult(output="Playing in browser.")

    monkeypatch.setattr(J, "_play_in_browser", _fake_browser)
    res = await J.play(_ctx(_cfg()), item_id="abc", title="Some Movie")
    assert called.get("hit") and res.success


# -- cast-movie duck exclusion ----------------------------------------------

def test_cast_duck_exclude_match_reads_config():
    assert _dk._cast_duck_exclude_match(_ctx(_cfg(cast_excl="JellyfinMediaPlayer"))) == "jellyfinmediaplayer"
    assert _dk._cast_duck_exclude_match(_ctx(_cfg())) == ""   # empty default


_SINKS = (
    'Sink Input #10\n\tCorked: no\n'
    '\tVolume: front-left: 65536 / 100% / 0 dB\n'
    '\tProperties:\n\t\tapplication.name = "Firefox"\n\t\tnode.name = "Firefox"\n'
    'Sink Input #20\n\tCorked: no\n'
    '\tVolume: front-left: 65536 / 80% / 0 dB\n'
    '\tProperties:\n\t\tapplication.name = "mpv Media Player"\n\t\tnode.name = "mpv"\n'
)


@pytest.mark.asyncio
async def test_full_mute_excludes_cast_movie_node_but_ducks_others(monkeypatch):
    # With jellyfin.cast_duck_exclude_match="mpv" set, the brain's full-mute local-duck must skip the cast
    # movie sink (the belt owns it) while still hard-muting other local media (the Firefox tab).
    monkeypatch.setattr(_dk.shutil, "which", lambda _: "/usr/bin/pactl")
    set_calls = []

    async def _fake_pactl(*args):
        if args[:2] == ("list", "sink-inputs"):
            return (0, _SINKS)
        if args and args[0] == "set-sink-input-volume":
            set_calls.append((args[1], args[2]))
            return (0, "")
        return (1, "")

    monkeypatch.setattr(_dk, "_run_pactl", _fake_pactl)
    ctx = _ctx(_cfg(cast_excl="mpv"))
    acted = await _dk._duck_local_sinks(ctx, on=True, mute=True)
    assert acted is True
    muted_idxs = {idx for idx, vol in set_calls if vol == "0%"}
    assert "10" in muted_idxs        # Firefox tab hard-muted
    assert "20" not in muted_idxs    # mpv cast movie excluded — belt owns it


@pytest.mark.asyncio
async def test_full_mute_no_exclude_match_ducks_everything(monkeypatch):
    # Empty default ⇒ no extra exclusion ⇒ EM/Pi behavior: every non-excluded local sink is hard-muted.
    monkeypatch.setattr(_dk.shutil, "which", lambda _: "/usr/bin/pactl")
    set_calls = []

    async def _fake_pactl(*args):
        if args[:2] == ("list", "sink-inputs"):
            return (0, _SINKS)
        if args and args[0] == "set-sink-input-volume":
            set_calls.append((args[1], args[2]))
            return (0, "")
        return (1, "")

    monkeypatch.setattr(_dk, "_run_pactl", _fake_pactl)
    ctx = _ctx(_cfg())   # no cast_duck_exclude_match
    await _dk._duck_local_sinks(ctx, on=True, mute=True)
    muted_idxs = {idx for idx, vol in set_calls if vol == "0%"}
    assert {"10", "20"} <= muted_idxs   # both ducked when nothing is excluded
