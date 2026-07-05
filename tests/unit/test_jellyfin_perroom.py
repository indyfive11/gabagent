"""Per-room Jellyfin (#62 video half): resolve_jellyfin fallthrough, target-session match, locality
override, and play()/control() cast routing. Deployment = ONE shared EM server; rooms differ only by
the client they cast to (jellyfin_client_target)."""
from types import SimpleNamespace

import pytest

from gabagent.commands.providers import jellyfin as J
from gabagent.config.models import GabAgentConfig, JellyfinConfig, RoomMediaProfile


def _cfg(room_media=None):
    return GabAgentConfig(
        api_key="t",
        jellyfin=JellyfinConfig(base_url="http://em:8096", api_key="EMKEY"),
        room_media=room_media or {},
    )


def _ctx(cfg, room_id=None, **extra):
    return SimpleNamespace(config=cfg, room_id=room_id, **extra)


@pytest.fixture(autouse=True)
def _force_kde(monkeypatch):
    # The browser-fallback test here runs the KDE/EM owned-browser path; force KDE so the new non-KDE guard
    # (jellyfin._is_kde_wayland_desktop) doesn't short-circuit it in a headless/non-KDE test env. The cast
    # path tests don't reach the guard, so this is harmless for them.
    monkeypatch.setattr(J, "_is_kde_wayland_desktop", lambda: True)


# -- resolve_jellyfin -------------------------------------------------------

def test_resolve_no_room_is_global():
    jc = J.resolve_jellyfin(_ctx(_cfg()))
    assert jc.base_url == "http://em:8096" and jc.api_key == "EMKEY"


def test_resolve_client_target_only_falls_through_to_shared_server():
    # The real deployment: raspi sets ONLY client_target → server stays the shared EM one.
    rm = {"raspi": RoomMediaProfile(jellyfin_client_target="raspi-jellyfin", duck_local=True)}
    jc = J.resolve_jellyfin(_ctx(_cfg(rm), room_id="raspi"))
    assert jc.base_url == "http://em:8096" and jc.api_key == "EMKEY"   # unchanged


def test_resolve_base_url_override_applies():
    # The speculative multi-server hook: if a room DOES override base_url, model_copy applies it.
    rm = {"den": RoomMediaProfile(jellyfin_base_url="http://den:8096")}
    jc = J.resolve_jellyfin(_ctx(_cfg(rm), room_id="den"))
    assert jc.base_url == "http://den:8096" and jc.api_key == "EMKEY"   # key falls through


# -- target-session matching ------------------------------------------------

def test_match_target_session_devicename_first_then_client():
    sessions = [
        {"Id": "A", "DeviceName": "Living Room TV", "Client": "Jellyfin Web"},
        {"Id": "B", "DeviceName": "raspi-jellyfin", "Client": "mpv-shim"},
    ]
    assert J._match_target_session(sessions, "raspi-jellyfin")["Id"] == "B"
    assert J._match_target_session(sessions, "RASPI-JELLYFIN")["Id"] == "B"   # case-insensitive
    assert J._match_target_session(sessions, "mpv-shim")["Id"] == "B"         # Client fallback
    assert J._match_target_session(sessions, "nope") is None
    assert J._match_target_session(sessions, "") is None


# -- locality override (the duck enabler) -----------------------------------

def test_session_locality_local_for_duck_local_target():
    rm = {"raspi": RoomMediaProfile(jellyfin_client_target="raspi-jellyfin", duck_local=True)}
    ctx = _ctx(_cfg(rm), room_id="raspi")
    # The room's own mpv-shim session — would read REMOTE by endpoint, but is forced LOCAL.
    s = {"DeviceName": "raspi-jellyfin", "RemoteEndPoint": "192.168.1.108:5000", "DeviceId": "x"}
    assert J._session_locality(ctx, s) == "local"
    # A different session in the same room stays the pure global verdict (remote, LAN endpoint).
    other = {"DeviceName": "Some TV", "RemoteEndPoint": "192.168.1.50:5000", "DeviceId": "y"}
    assert J._session_locality(ctx, other) == "remote"


def test_session_locality_unaffected_without_duck_local():
    rm = {"raspi": RoomMediaProfile(jellyfin_client_target="raspi-jellyfin", duck_local=False)}
    ctx = _ctx(_cfg(rm), room_id="raspi")
    s = {"DeviceName": "raspi-jellyfin", "RemoteEndPoint": "192.168.1.108:5000", "DeviceId": "x"}
    assert J._session_locality(ctx, s) == "remote"   # no per-room locality without duck_local


# -- locality flap-hardening: remembered DeviceId pins the cast session local through the L↔R flap ------

def test_seed_remembers_target_deviceid():
    rm = {"raspi": RoomMediaProfile(jellyfin_client_target="raspi-jellyfin", duck_local=True)}
    ctx = _ctx(_cfg(rm), room_id="raspi")
    J._seed_owned_deviceid(ctx, [{"DeviceName": "raspi-jellyfin", "DeviceId": "01ad147b"}])
    assert J._owned_deviceid_cache(ctx).get("raspi") == "01ad147b"


def test_seed_from_idle_session_without_nowplaying():
    """Seed scans the FULL /Sessions list, so it remembers the DeviceId even when the target is idle
    (no NowPlayingItem) — so the DeviceId is known before playback flaps the locality verdict."""
    rm = {"raspi": RoomMediaProfile(jellyfin_client_target="raspi-jellyfin", duck_local=True)}
    ctx = _ctx(_cfg(rm), room_id="raspi")
    # No NowPlayingItem key at all — still seeded.
    J._seed_owned_deviceid(ctx, [{"DeviceName": "raspi-jellyfin", "DeviceId": "01ad147b"}])
    assert J._owned_deviceid_cache(ctx).get("raspi") == "01ad147b"


def test_session_locality_pins_by_remembered_deviceid_through_flap():
    """The fix: after seeding, a poll where the SAME session (by stable DeviceId) reports a blank/mismatched
    DeviceName + LAN endpoint (the cast-negotiation flap) still reads LOCAL — killing the L↔R flap that made
    /media/state report the movie idle."""
    rm = {"raspi": RoomMediaProfile(jellyfin_client_target="raspi-jellyfin", duck_local=True)}
    ctx = _ctx(_cfg(rm), room_id="raspi")
    J._seed_owned_deviceid(ctx, [{"DeviceName": "raspi-jellyfin", "DeviceId": "01ad147b"}])
    flapped = {"DeviceName": "", "DeviceId": "01ad147b", "RemoteEndPoint": "192.168.1.108:5000"}
    assert J._session_locality(ctx, flapped) == "local"


def test_remembered_deviceid_does_not_leak_to_other_sessions():
    """A genuinely different remote session (different DeviceId) must stay remote — the pin is exact."""
    rm = {"raspi": RoomMediaProfile(jellyfin_client_target="raspi-jellyfin", duck_local=True)}
    ctx = _ctx(_cfg(rm), room_id="raspi")
    J._seed_owned_deviceid(ctx, [{"DeviceName": "raspi-jellyfin", "DeviceId": "01ad147b"}])
    other = {"DeviceName": "Living Room TV", "DeviceId": "ffff", "RemoteEndPoint": "192.168.1.50:5000"}
    assert J._session_locality(ctx, other) == "remote"


def test_seed_noop_without_duck_local():
    rm = {"raspi": RoomMediaProfile(jellyfin_client_target="raspi-jellyfin", duck_local=False)}
    ctx = _ctx(_cfg(rm), room_id="raspi")
    J._seed_owned_deviceid(ctx, [{"DeviceName": "raspi-jellyfin", "DeviceId": "01ad147b"}])
    assert J._owned_deviceid_cache(ctx).get("raspi") is None


def test_seed_refreshes_on_shim_restart():
    """A shim restart re-registers under a new DeviceId — the seed refreshes each poll, never goes stale."""
    rm = {"raspi": RoomMediaProfile(jellyfin_client_target="raspi-jellyfin", duck_local=True)}
    ctx = _ctx(_cfg(rm), room_id="raspi")
    J._seed_owned_deviceid(ctx, [{"DeviceName": "raspi-jellyfin", "DeviceId": "old"}])
    J._seed_owned_deviceid(ctx, [{"DeviceName": "raspi-jellyfin", "DeviceId": "new"}])
    assert J._owned_deviceid_cache(ctx).get("raspi") == "new"


def test_seed_noop_for_default_room_no_target():
    """EM/default (no room_id, no target) — seed is a pure no-op, cache stays empty (byte-identical)."""
    ctx = _ctx(_cfg(), room_id=None)
    J._seed_owned_deviceid(ctx, [{"DeviceName": "raspi-jellyfin", "DeviceId": "01ad147b"}])
    assert J._owned_deviceid_cache(ctx) == {}


# -- play() cast routing ----------------------------------------------------

@pytest.mark.asyncio
async def test_play_casts_to_room_session(monkeypatch):
    rm = {"raspi": RoomMediaProfile(jellyfin_client_target="raspi-jellyfin", duck_local=True)}
    ctx = _ctx(_cfg(rm), room_id="raspi")
    called = {}

    async def fake_sessions(jc):
        return [{"Id": "S1", "DeviceName": "raspi-jellyfin"}]

    async def fake_to_session(jc, session_id, item_id, label, start_secs=None):
        called.update(session_id=session_id, item_id=item_id, label=label)
        from gabagent.api.models import ToolResult
        return ToolResult(output=f"Playing on {label}.")

    async def fail_browser(*a, **k):
        raise AssertionError("must NOT open the brain-host browser for a per-room target")

    monkeypatch.setattr(J, "_sessions", fake_sessions)
    monkeypatch.setattr(J, "_play_to_session", fake_to_session)
    monkeypatch.setattr(J, "_play_in_browser", fail_browser)
    res = await J.play(ctx, item_id="movie42", title="Dune")
    assert res.success and called == {"session_id": "S1", "item_id": "movie42", "label": "raspi-jellyfin"}
    assert ctx.jellyfin_playing_title == "Dune"


@pytest.mark.asyncio
async def test_play_errors_when_target_session_absent(monkeypatch):
    rm = {"raspi": RoomMediaProfile(jellyfin_client_target="raspi-jellyfin", duck_local=True)}
    ctx = _ctx(_cfg(rm), room_id="raspi")

    async def no_sessions(jc):
        return []

    async def fail_browser(*a, **k):
        raise AssertionError("must NOT fall back to a brain-host browser for a satellite room")

    monkeypatch.setattr(J, "_sessions", no_sessions)
    monkeypatch.setattr(J, "_play_in_browser", fail_browser)
    res = await J.play(ctx, item_id="movie42", title="Dune")
    assert not res.success and "isn't available" in res.error


@pytest.mark.asyncio
async def test_play_no_target_uses_browser(monkeypatch):
    ctx = _ctx(_cfg(), room_id=None)   # EM / default: no per-room target
    used = {}

    async def fake_browser(ctx, jc, item_id, title="", start_secs=None):
        used.update(item_id=item_id, title=title)
        from gabagent.api.models import ToolResult
        return ToolResult(output="opened browser")

    monkeypatch.setattr(J, "_play_in_browser", fake_browser)
    res = await J.play(ctx, item_id="m", title="T")
    assert res.success and used == {"item_id": "m", "title": "T"}
