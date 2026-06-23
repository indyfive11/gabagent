"""Phase 10 / #62 — per-room media target resolution. `resolve_tidal(ctx)` returns this room's Mopidy
endpoint (config.room_media[room_id].tidal_rpc_url) for BOTH control and the RPC duck, falling through to
the global tidal config when there's no room / no profile / no override — so an unconfigured install is
byte-identical to pre-#62."""
import types

import pytest

from gabagent.config.models import GabAgentConfig, RoomMediaProfile
from gabagent.commands.providers.tidal import resolve_tidal

PI = "http://192.168.1.108:6680/mopidy/rpc"
GLOBAL = "http://localhost:6680/mopidy/rpc"


def _ctx(cfg, room_id=None):
    return types.SimpleNamespace(config=cfg, room_id=room_id)


# --- config model ---------------------------------------------------------

def test_config_defaults_are_noop():
    cfg = GabAgentConfig(api_key="t")
    assert cfg.room_media == {}                       # empty = no overrides anywhere
    assert RoomMediaProfile().tidal_rpc_url == ""     # empty profile = fall through


def test_room_media_coerces_dict_to_profile():
    cfg = GabAgentConfig(api_key="t", room_media={"raspi": {"tidal_rpc_url": PI}})
    assert isinstance(cfg.room_media["raspi"], RoomMediaProfile)
    assert cfg.room_media["raspi"].tidal_rpc_url == PI


# --- resolver: fall-through (byte-identical) cases ------------------------

def test_no_room_id_returns_global_object():
    cfg = GabAgentConfig(api_key="t")
    tc = resolve_tidal(_ctx(cfg, None))
    assert tc is cfg.tidal                            # same object, no copy
    assert tc.rpc_url == GLOBAL


def test_room_without_profile_returns_global():
    cfg = GabAgentConfig(api_key="t", room_media={"raspi": {"tidal_rpc_url": PI}})
    tc = resolve_tidal(_ctx(cfg, "EndeavorMain"))     # a different room, no entry
    assert tc is cfg.tidal


def test_empty_override_returns_global():
    cfg = GabAgentConfig(api_key="t", room_media={"raspi": {"tidal_rpc_url": ""}})
    assert resolve_tidal(_ctx(cfg, "raspi")) is cfg.tidal


def test_none_when_tidal_unconfigured():
    ctx = types.SimpleNamespace(
        config=types.SimpleNamespace(tidal=None, room_media={}), room_id="raspi")
    assert resolve_tidal(ctx) is None


# --- resolver: the override case ------------------------------------------

def test_override_redirects_rpc_url_only():
    cfg = GabAgentConfig(api_key="t", room_media={"raspi": {"tidal_rpc_url": PI}})
    tc = resolve_tidal(_ctx(cfg, "raspi"))
    assert tc.rpc_url == PI
    assert tc.enabled == cfg.tidal.enabled            # other fields preserved


def test_override_does_not_mutate_global():
    cfg = GabAgentConfig(api_key="t", room_media={"raspi": {"tidal_rpc_url": PI}})
    _ = resolve_tidal(_ctx(cfg, "raspi"))
    assert cfg.tidal.rpc_url == GLOBAL                # the shared global stays put (model_copy, not in-place)


# --- #2 per-room RPC timeout override -------------------------------------

def test_rpc_timeout_defaults_to_zero():
    cfg = GabAgentConfig(api_key="t")
    assert cfg.tidal.rpc_timeout == 0.0               # 0 ⇒ module default (_RPC_TIMEOUT)
    assert RoomMediaProfile().tidal_rpc_timeout == 0.0


def test_room_timeout_override_applied():
    cfg = GabAgentConfig(api_key="t", room_media={"raspi": {"tidal_rpc_timeout": 90.0}})
    tc = resolve_tidal(_ctx(cfg, "raspi"))
    assert tc.rpc_timeout == 90.0
    assert tc.rpc_url == GLOBAL                        # endpoint untouched when only the timeout is set
    assert cfg.tidal.rpc_timeout == 0.0               # global not mutated


def test_room_timeout_and_url_override_together():
    cfg = GabAgentConfig(api_key="t",
                         room_media={"raspi": {"tidal_rpc_url": PI, "tidal_rpc_timeout": 90.0}})
    tc = resolve_tidal(_ctx(cfg, "raspi"))
    assert tc.rpc_url == PI and tc.rpc_timeout == 90.0


def test_rpc_timeout_resolution_helper():
    from gabagent.commands.providers.tidal import _rpc_timeout, _RPC_TIMEOUT
    import types as _t
    assert _rpc_timeout(_t.SimpleNamespace(rpc_timeout=0.0), None) == _RPC_TIMEOUT  # 0 ⇒ default
    assert _rpc_timeout(_t.SimpleNamespace(rpc_timeout=90.0), None) == 90.0         # room budget
    assert _rpc_timeout(_t.SimpleNamespace(rpc_timeout=90.0), 2.0) == 2.0           # explicit arg wins


# --- the duck rides the same resolved endpoint (the #62 consensus claim) --

async def test_duck_local_skips_mixer_rpc_for_that_room(monkeypatch):
    """room_media[room].duck_local=True ⇒ the brain's _duck_tidal short-circuits: NO mixer-RPC, returns
    False (so duck_media won't report ducked:['tidal']), and emits a skip receipt. The satellite's
    sink belt owns the duck. (#62 duck-decoupling fix.)"""
    from gabagent.commands.providers import tidal as tidalmod
    from gabagent.voice import ducking
    calls = []

    async def fake_rpc(tc, method, params=None, timeout=None):
        calls.append(method)
        return "playing"
    monkeypatch.setattr(tidalmod, "_rpc", fake_rpc)
    logged = []
    monkeypatch.setattr(ducking, "_tidal_dlog", lambda ctx, **k: logged.append(k))

    cfg = GabAgentConfig(api_key="t", room_media={"raspi": {"tidal_rpc_url": PI, "duck_local": True}})
    ctx = types.SimpleNamespace(config=cfg, room_id="raspi")
    assert await ducking._duck_tidal(ctx, on=True) is False
    assert await ducking._duck_tidal(ctx, on=False) is False
    assert calls == []                                          # the mixer-RPC never fired
    assert all(k.get("phase") == "skip_duck_local" for k in logged) and len(logged) == 2


async def test_duck_local_default_false_still_ducks(monkeypatch):
    """Without duck_local, the brain ducks via mixer-RPC as before — the flag is opt-in per room."""
    from gabagent.commands.providers import tidal as tidalmod
    from gabagent.voice import ducking
    calls = []

    async def fake_rpc(tc, method, params=None, timeout=None):
        calls.append(method)
        if method == "core.playback.get_state":
            return "playing"
        if method == "core.mixer.get_volume":
            return 60
        return None
    monkeypatch.setattr(tidalmod, "_rpc", fake_rpc)
    monkeypatch.setattr(ducking, "_tidal_dlog", lambda *a, **k: None)
    async def _noop_sink(*a, **k):
        return None
    monkeypatch.setattr(ducking, "_duck_tidal_sink", _noop_sink)

    cfg = GabAgentConfig(api_key="t", room_media={"raspi": {"tidal_rpc_url": PI}})   # no duck_local
    ctx = types.SimpleNamespace(config=cfg, room_id="raspi")
    assert await ducking._duck_tidal(ctx, on=True) is True
    assert "core.mixer.set_volume" in calls                     # ducked normally


async def test_room_media_duck_local_coerces_and_defaults():
    cfg = GabAgentConfig(api_key="t", room_media={"raspi": {"duck_local": True}})
    assert cfg.room_media["raspi"].duck_local is True
    assert RoomMediaProfile().duck_local is False               # default off


async def test_duck_targets_the_resolved_room_endpoint(monkeypatch):
    from gabagent.commands.providers import tidal as tidalmod
    from gabagent.voice import ducking
    seen = []

    async def fake_rpc(tc, method, params=None, timeout=None):
        seen.append(getattr(tc, "rpc_url", None))
        if method == "core.playback.get_state":
            return "playing"
        if method == "core.mixer.get_volume":
            return 80
        return None

    monkeypatch.setattr(tidalmod, "_rpc", fake_rpc)
    monkeypatch.setattr(ducking, "_tidal_dlog", lambda *a, **k: None)

    async def _noop_sink(*a, **k):
        return None
    monkeypatch.setattr(ducking, "_duck_tidal_sink", _noop_sink)

    cfg = GabAgentConfig(api_key="t", room_media={"raspi": {"tidal_rpc_url": PI}})
    ctx = types.SimpleNamespace(config=cfg, room_id="raspi")
    ok = await ducking._duck_tidal(ctx, on=True)
    assert ok is True
    assert seen and all(url == PI for url in seen)    # every duck RPC hit the Pi's Mopidy, not localhost
