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
    assert await resolve.resolve_media_intent(None, "jellyfin.now_playing") is None
    assert await resolve.resolve_media_intent(None, "weather.today") is None


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
