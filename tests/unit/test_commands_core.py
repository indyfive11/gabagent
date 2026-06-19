import types
from pathlib import Path
import httpx
import respx
import pytest

from gabagent.commands.model import Command, Slot, ShellBackend, HttpBackend, Detect
from gabagent.commands.catalog import CommandCatalog
from gabagent.commands.backends import run_backend, validate_and_resolve
from gabagent.commands.discovery import discover_capabilities
from gabagent.commands.tools import RunCommandTool, ListCapabilitiesTool
from gabagent.permissions.tiers import tier_of


def _echo_cmd(tier=1):
    return Command(
        id="test.echo", domain="test", summary="echo a value", tier=tier,
        backend=ShellBackend(argv=["echo", "{val}"]),
        params=[Slot(name="val", type="string", required=True)],
    )


# -- slot validation / injection safety -----------------------------------

def test_unknown_param_rejected():
    with pytest.raises(ValueError):
        validate_and_resolve(_echo_cmd(), {"val": "x", "bogus": 1})


def test_paramless_command_ignores_stray_args():
    # A fixed-action command with no declared params (e.g. system.volume_up = pactl +10%) tolerates a
    # stray arg the model invents (`volume_up(level=50)`) instead of failing the turn.
    cmd = Command(id="system.volume_up", domain="system", summary="louder", tier=1,
                  backend=ShellBackend(argv=["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+10%"]))
    assert validate_and_resolve(cmd, {"level": 50, "unit": "percent"}) == {}


def test_missing_required_rejected():
    with pytest.raises(ValueError):
        validate_and_resolve(_echo_cmd(), {})


def test_enum_violation_rejected():
    cmd = Command(id="t", domain="d", summary="s", tier=1,
                  backend=ShellBackend(argv=["echo", "{m}"]),
                  params=[Slot(name="m", type="enum", enum=("a", "b"), required=True)])
    with pytest.raises(ValueError):
        validate_and_resolve(cmd, {"m": "c"})


async def test_shell_value_stays_one_token_no_shell():
    # A value full of shell metachars must be echoed literally, proving it never hit a shell.
    res = await run_backend(_echo_cmd(), {"val": "; rm -rf / && echo pwned"}, ctx=None)
    assert res.success
    assert res.output == "; rm -rf / && echo pwned"


async def test_shell_command_not_found():
    cmd = Command(id="t", domain="d", summary="s", tier=1,
                  backend=ShellBackend(argv=["definitely-not-a-real-binary-xyz"]))
    res = await run_backend(cmd, {}, ctx=None)
    assert not res.success and "not found" in res.error


# -- http backend ----------------------------------------------------------

@respx.mock
async def test_http_backend_full_url():
    respx.get("http://test.local/api").mock(return_value=httpx.Response(200, text="ok-body"))
    cmd = Command(id="t", domain="d", summary="s", tier=1,
                  backend=HttpBackend(method="GET", path="http://test.local/api"))
    res = await run_backend(cmd, {}, ctx=types.SimpleNamespace(config=types.SimpleNamespace()))
    assert res.success and "ok-body" in res.output


# -- catalog + discovery ---------------------------------------------------

def test_catalog_summaries():
    cat = CommandCatalog()
    cat.add(_echo_cmd(tier=2))
    s = cat.summaries()
    assert s[0]["id"] == "test.echo" and s[0]["tier"] == 2
    assert s[0]["params"][0]["name"] == "val"


async def test_discovery_with_fake_providers():
    class Good:
        id = "good"
        async def detect(self, ctx): return True
        def commands(self, ctx): return [_echo_cmd()]

    class Absent:
        id = "absent"
        async def detect(self, ctx): return False
        def commands(self, ctx): return [_echo_cmd()]

    class Broken:
        id = "broken"
        async def detect(self, ctx): raise RuntimeError("boom")
        def commands(self, ctx): return []

    ctx = types.SimpleNamespace(config=types.SimpleNamespace(commands_enabled=True))
    cat = await discover_capabilities(ctx, providers=[Good(), Absent(), Broken()])
    assert cat.ids() == ["test.echo"]  # only the detected one; broken didn't abort


# -- tier resolution from the catalog -------------------------------------

def test_tier_of_run_command_uses_catalog(tmp_path):
    cat = CommandCatalog()
    cat.add(_echo_cmd(tier=2))
    assert tier_of("run_command", {"command_id": "test.echo"}, tmp_path, None, cat) == 2
    # Unknown id / no catalog → Tier 1: it can't execute, so no point prompting the user
    # (the run_command tool rejects it and the model self-corrects).
    assert tier_of("run_command", {"command_id": "nope"}, tmp_path, None, cat) == 1
    assert tier_of("run_command", {"command_id": "test.echo"}, tmp_path, None, None) == 1
    assert tier_of("list_capabilities", {}, tmp_path, None, cat) == 1


def test_tier_of_direct_command_id_mis_call_resolves_to_catalog_tier(tmp_path):
    # The model mis-calls a command as a DIRECT tool (e.g. tidal.recommendations instead of
    # run_command(command_id=...)). It must resolve to the command's catalog tier, NOT fail-closed to Tier-3
    # — which gated a safe "pick & play" behind a mouse-click while the correct path was Tier-1/auto
    # (Rob, live 2026-06-15).
    cat = CommandCatalog()
    cat.add(_echo_cmd(tier=1))
    assert tier_of("test.echo", {}, tmp_path, None, cat) == 1        # resolved to catalog tier, not 3
    # A truly unknown tool (not a command id) still fails closed.
    assert tier_of("definitely_not_a_tool", {}, tmp_path, None, cat) == 3


# -- run_command tool dispatch --------------------------------------------

async def test_run_command_tool(tmp_path):
    cat = CommandCatalog()
    cat.add(_echo_cmd())
    ctx = types.SimpleNamespace(command_catalog=cat, config=types.SimpleNamespace())
    res = await RunCommandTool().execute(ctx, command_id="test.echo", args={"val": "hi"})
    assert res.success and res.output == "hi"

    res2 = await RunCommandTool().execute(ctx, command_id="missing", args={})
    assert not res2.success and "Unknown command" in res2.error


async def test_list_capabilities_tool(tmp_path):
    cat = CommandCatalog()
    cat.add(_echo_cmd())
    ctx = types.SimpleNamespace(command_catalog=cat)
    res = await ListCapabilitiesTool().execute(ctx)
    assert "test.echo" in res.output

    empty = types.SimpleNamespace(command_catalog=None)
    res2 = await ListCapabilitiesTool().execute(empty)
    assert "No device/media capabilities" in res2.output


# -- indexed capability exposure (scales to many skills) -------------------

def _cat_with(n_per_domain=2, domains=("media", "window", "system")):
    from gabagent.commands.model import Command, ShellBackend
    cat = CommandCatalog()
    for d in domains:
        for i in range(n_per_domain):
            cat.add(Command(id=f"{d}.cmd{i}", domain=d, tier=1, summary=f"{d} thing {i}",
                            backend=ShellBackend(argv=["true"]), examples=[f"do {d} {i}"]))
    return cat


def test_catalog_index_is_per_domain():
    cat = _cat_with(n_per_domain=3, domains=("media", "window"))
    idx = cat.index()
    assert {d["domain"] for d in idx} == {"media", "window"}
    media = next(d for d in idx if d["domain"] == "media")
    assert media["count"] == 3 and len(media["commands"]) == 3 and "params" not in media


def test_catalog_search_ranks_matches():
    from gabagent.commands.model import Command, ShellBackend
    cat = CommandCatalog()
    cat.add(Command(id="tidal.play", domain="media", tier=1, summary="Play music on TIDAL",
                    backend=ShellBackend(argv=["true"]), examples=["play some music"]))
    cat.add(Command(id="window.close", domain="window", tier=1, summary="Close the active window",
                    backend=ShellBackend(argv=["true"])))
    hits = [c.id for c in cat.search("play music")]
    assert hits and hits[0] == "tidal.play" and "window.close" not in hits


async def test_list_capabilities_blank_returns_index_not_everything():
    import types, json
    from gabagent.commands.tools import ListCapabilitiesTool
    ctx = types.SimpleNamespace(command_catalog=_cat_with(n_per_domain=5))
    out = json.loads((await ListCapabilitiesTool().execute(ctx)).output)
    assert all("domain" in d and "count" in d and "params" not in d for d in out)  # index, not full dump


async def test_list_capabilities_query_and_domain_drill_down():
    import types, json
    from gabagent.commands.tools import ListCapabilitiesTool
    ctx = types.SimpleNamespace(command_catalog=_cat_with(n_per_domain=2))
    by_domain = json.loads((await ListCapabilitiesTool().execute(ctx, domain="media")).output)
    assert by_domain and all(r["domain"] == "media" and "params" in r for r in by_domain)
    by_query = json.loads((await ListCapabilitiesTool().execute(ctx, query="window")).output)
    assert by_query and all(r["domain"] == "window" for r in by_query)


def test_index_stays_flat_as_commands_scale():
    # The brief must not grow with command count — only with domains.
    from gabagent.voice.turn import _capability_brief
    import types
    small = types.SimpleNamespace(command_catalog=_cat_with(n_per_domain=2))
    huge = types.SimpleNamespace(command_catalog=_cat_with(n_per_domain=40))   # 120 commands
    b_small = _capability_brief(small)
    b_huge = _capability_brief(huge)
    # Same domains → same domain-index lines; hot set capped → length barely changes.
    assert abs(len(b_huge) - len(b_small)) < 200
    assert len(b_huge) < 1200   # nowhere near a 120-command flat dump
