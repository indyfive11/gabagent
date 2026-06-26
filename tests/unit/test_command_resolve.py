"""Forgiving run_command resolution: media-intent → active provider, plus difflib did-you-mean."""
import types
import pytest

from gabagent.commands import resolve


class _Src:
    def __init__(self, provider, state):
        self.provider = provider
        self.state = state


@pytest.fixture
def patch_inv(monkeypatch):
    def _set(srcs):
        async def fake_inventory(ctx):
            return srcs
        monkeypatch.setattr("gabagent.commands.media.inventory", fake_inventory, raising=False)
        monkeypatch.setattr("gabagent.commands.media.local_audible", lambda s: s, raising=False)
    return _set


@pytest.mark.asyncio
@pytest.mark.parametrize("cid,expect", [
    ("media.playpause", "tidal.pause"),   # playing → toggle resolves to pause
    ("media.pause", "tidal.pause"),
    ("media.play", "tidal.resume"),
    ("media.stop", "tidal.stop"),
    ("media.next", "tidal.next"),
    ("media.previous", "tidal.previous"),
    ("jellyfin.pause", "tidal.pause"),    # wrong provider name, but Tidal is what's playing
    ("system.skip", "tidal.next"),
])
async def test_media_intent_routes_to_playing_tidal(patch_inv, cid, expect):
    patch_inv([_Src("tidal", "playing")])
    res = await resolve.resolve_media_intent(None, cid)
    assert res is not None and res[0] == expect and res[1] == {}


@pytest.mark.asyncio
async def test_toggle_resumes_when_paused(patch_inv):
    patch_inv([_Src("tidal", "paused")])
    res = await resolve.resolve_media_intent(None, "media.playpause")
    assert res[0] == "tidal.resume"


@pytest.mark.asyncio
async def test_jellyfin_routes_through_control_action(patch_inv):
    patch_inv([_Src("jellyfin", "playing")])
    assert await resolve.resolve_media_intent(None, "media.pause") == ("jellyfin.control", {"action": "pause"})
    assert await resolve.resolve_media_intent(None, "media.previous") == ("jellyfin.control", {"action": "back"})


@pytest.mark.asyncio
async def test_no_resolution_when_nothing_playing(patch_inv):
    patch_inv([])
    assert await resolve.resolve_media_intent(None, "media.pause") is None


@pytest.mark.asyncio
async def test_non_transport_ids_are_ignored(patch_inv):
    patch_inv([_Src("tidal", "playing")])
    assert await resolve.resolve_media_intent(None, "tidal.search") is None
    assert await resolve.resolve_media_intent(None, "weather.today") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cid", ["media.now_playing", "media.current", "media.nowplaying",
                                 "media.whats_playing", "music.current_track"])
async def test_now_playing_routes_to_active_provider(patch_inv, cid):
    # The model riffs media.now_playing / media.current; route it to whatever's actually playing.
    patch_inv([_Src("tidal", "playing")])
    assert await resolve.resolve_media_intent(None, cid) == ("tidal.now_playing", {})
    patch_inv([_Src("jellyfin", "paused")])
    assert await resolve.resolve_media_intent(None, cid) == ("jellyfin.now_playing", {})


@pytest.mark.asyncio
async def test_now_playing_unknown_provider_falls_through(patch_inv):
    patch_inv([_Src("spotify", "playing")])  # no now_playing mapping → None → difflib salvage
    assert await resolve.resolve_media_intent(None, "media.now_playing") is None


def test_closest_and_ratio():
    ids = ["tidal.pause", "tidal.resume", "jellyfin.control", "window.to_screen"]
    near = resolve.closest_command_ids("tidal.paus", ids)
    assert near and near[0] == "tidal.pause"
    assert resolve.best_match_ratio("tidal.paus", "tidal.pause") >= resolve.AUTO_ROUTE_RATIO


class _StubCatalog:
    def __init__(self, ids):
        self._ids = set(ids)
    def get(self, cid):
        return types.SimpleNamespace(id=cid) if cid in self._ids else None
    def ids(self):
        return list(self._ids)


@pytest.mark.asyncio
async def test_execute_routes_media_guess_to_active_provider(patch_inv, monkeypatch):
    from gabagent.commands.tools import RunCommandTool
    from gabagent.api.models import ToolResult
    patch_inv([_Src("tidal", "playing")])
    ran = {}
    async def fake_run_backend(cmd, args, ctx):
        ran["id"] = cmd.id
        return ToolResult(output="ok")
    monkeypatch.setattr("gabagent.commands.backends.run_backend", fake_run_backend, raising=False)
    monkeypatch.setattr("gabagent.commands.usage.record", lambda *a, **k: None, raising=False)
    ctx = types.SimpleNamespace(command_catalog=_StubCatalog({"tidal.pause", "tidal.resume"}))
    await RunCommandTool().execute(ctx, command_id="media.playpause", args={})
    assert ran["id"] == "tidal.pause"


@pytest.mark.asyncio
async def test_execute_autoroutes_close_typo(patch_inv, monkeypatch):
    from gabagent.commands.tools import RunCommandTool
    from gabagent.api.models import ToolResult
    patch_inv([])  # not a media turn
    ran = {}
    async def fake_run_backend(cmd, args, ctx):
        ran["id"] = cmd.id
        return ToolResult(output="ok")
    monkeypatch.setattr("gabagent.commands.backends.run_backend", fake_run_backend, raising=False)
    monkeypatch.setattr("gabagent.commands.usage.record", lambda *a, **k: None, raising=False)
    ctx = types.SimpleNamespace(command_catalog=_StubCatalog({"jellyfin.now_playing", "tidal.search"}))
    await RunCommandTool().execute(ctx, command_id="jellyfin.now_playng", args={})  # typo
    assert ran["id"] == "jellyfin.now_playing"


@pytest.mark.asyncio
async def test_execute_suggests_when_not_close(patch_inv, monkeypatch):
    from gabagent.commands.tools import RunCommandTool
    patch_inv([])
    ctx = types.SimpleNamespace(command_catalog=_StubCatalog({"tidal.search", "jellyfin.control"}))
    res = await RunCommandTool().execute(ctx, command_id="frobnicate.foo", args={})
    assert res.error and "Unknown command" in res.error


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["list_capabilities", "rescan_capabilities", "run_command"])
async def test_execute_redirects_wrapped_tool_name(patch_inv, tool_name):
    # Model wraps a top-level tool name in run_command → point it at the real tool, not a circular
    # "Unknown command: list_capabilities. Try list_capabilities." Catalog has no such id.
    from gabagent.commands.tools import RunCommandTool
    patch_inv([])
    ctx = types.SimpleNamespace(command_catalog=_StubCatalog({"tidal.search", "jellyfin.control"}))
    res = await RunCommandTool().execute(ctx, command_id=tool_name, args={})
    assert res.error and "its own tool" in res.error
    assert f"call {tool_name} directly" in res.error
    assert f"Try {tool_name}" not in res.error  # not the old self-defeating message


@pytest.mark.asyncio
@pytest.mark.parametrize("cid,expect_mode", [
    ("media.shuffle", "on"), ("music.randomize", "on"), ("tidal.shuffle_on", "on"),
    ("media.unshuffle", "off"), ("player.shuffle_off", "off"),
])
async def test_shuffle_routes_to_tidal_shuffle(patch_inv, cid, expect_mode):
    patch_inv([_Src("tidal", "playing")])
    res = await resolve.resolve_media_intent(None, cid)
    assert res == ("tidal.shuffle", {"mode": expect_mode})


@pytest.mark.asyncio
async def test_shuffle_falls_through_for_jellyfin(patch_inv):
    # No Jellyfin tracklist-shuffle equivalent we drive → don't route it.
    patch_inv([_Src("jellyfin", "playing")])
    assert await resolve.resolve_media_intent(None, "media.shuffle") is None


# --- salvage strictness is config-tunable (fat-thin `mobile` / small.en follow-up #2) -----------

def test_salvage_config_defaults_match_historical_constants():
    """An unconfigured install must behave EXACTLY as before — the config defaults equal the old
    bare constants (resolve.py 0.6/0.86, tidal 0.72)."""
    from gabagent.config.models import GabAgentConfig
    from gabagent.commands.providers.tidal import _PLAYLIST_PLAY_SCORE
    cfg = GabAgentConfig(api_key="test")
    assert cfg.salvage_command_cutoff == 0.6
    assert cfg.salvage_auto_route_ratio == resolve.AUTO_ROUTE_RATIO == 0.86
    assert cfg.salvage_playlist_play_score == _PLAYLIST_PLAY_SCORE == 0.72


@pytest.mark.asyncio
async def test_stricter_ratio_demotes_autoroute_to_suggest(patch_inv, monkeypatch):
    """A weak-STT `mobile` process raises salvage_auto_route_ratio → a borderline typo that auto-routes
    at the default 0.86 instead asks 'did you mean?' (the wrong-salvage guard, R7)."""
    from gabagent.commands.tools import RunCommandTool
    from gabagent.config.models import GabAgentConfig
    patch_inv([])
    monkeypatch.setattr("gabagent.commands.usage.record", lambda *a, **k: None, raising=False)
    cfg = GabAgentConfig(api_key="test", salvage_auto_route_ratio=0.99)
    ctx = types.SimpleNamespace(
        command_catalog=_StubCatalog({"jellyfin.now_playing", "tidal.search"}), config=cfg)
    res = await RunCommandTool().execute(ctx, command_id="jellyfin.now_playng", args={})  # typo, ratio 0.974
    assert res.error and "Did you mean" in res.error  # not silently auto-routed under the stricter ratio


@pytest.mark.asyncio
async def test_stricter_cutoff_drops_marginal_candidate(patch_inv):
    """A high salvage_command_cutoff filters a marginal near-match out entirely → plain unknown."""
    from gabagent.commands.tools import RunCommandTool
    from gabagent.config.models import GabAgentConfig
    patch_inv([])
    cfg = GabAgentConfig(api_key="test", salvage_command_cutoff=0.99, salvage_auto_route_ratio=0.99)
    ctx = types.SimpleNamespace(
        command_catalog=_StubCatalog({"jellyfin.now_playing", "tidal.search"}), config=cfg)
    res = await RunCommandTool().execute(ctx, command_id="jellyfin.now_playng", args={})
    assert res.error and "Try list_capabilities" in res.error
