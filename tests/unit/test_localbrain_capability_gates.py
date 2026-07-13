"""Local-brain transfer (2026-06-25): the two brain-side capability gates.

Fork C — `desktop._is_plasma()` must require a REAL Plasma/KWin marker, not bare `qdbus6` (a Qt6 dep
present on Cinnamon, whose WM is Muffin → KWin DBus calls fail). Else the laptop brain advertises window
control it can't execute (detect-true → call-fail).

Fork E — the media honesty gate (`turn._has_media_capability` + the `_voice_system` constraint) must mute
media over-claims on a device with no real player, while leaving media-capable / cast-target rooms alone.
"""
import types
from pathlib import Path

import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.agent.context import AgentContext
from gabagent.commands.providers.desktop import DesktopProvider, _is_plasma
from gabagent.voice.turn import _has_media_capability, _voice_system

_HONEST = "This device has NO media playback"


# ---- Fork C: Plasma detection ------------------------------------------------

def _only(*present):
    s = set(present)
    return lambda b: f"/usr/bin/{b}" if b in s else None


def test_is_plasma_false_with_only_qdbus6(monkeypatch):
    # The Cinnamon laptop: qdbus6 present (Qt6 dep), no Plasma/KWin.
    monkeypatch.setattr("gabagent.commands.providers.desktop.shutil.which", _only("qdbus6"))
    assert _is_plasma() is False


def test_is_plasma_true_with_kwin_or_plasmashell(monkeypatch):
    monkeypatch.setattr("gabagent.commands.providers.desktop.shutil.which", _only("plasmashell", "qdbus6"))
    assert _is_plasma() is True
    monkeypatch.setattr("gabagent.commands.providers.desktop.shutil.which", _only("kwin_wayland"))
    assert _is_plasma() is True


def test_is_plasma_false_with_nothing(monkeypatch):
    monkeypatch.setattr("gabagent.commands.providers.desktop.shutil.which", _only())
    assert _is_plasma() is False


@pytest.mark.asyncio
async def test_desktop_detect_false_on_cinnamon(monkeypatch):
    monkeypatch.setattr("gabagent.commands.providers.desktop.shutil.which", _only("qdbus6"))
    assert await DesktopProvider().detect(ctx=None) is False


@pytest.mark.asyncio
async def test_desktop_emits_no_window_commands_on_cinnamon(monkeypatch):
    # Even if commands() is reached, the qdbus6-only laptop yields NO window/desktop control commands.
    monkeypatch.setattr("gabagent.commands.providers.desktop.shutil.which", _only("qdbus6"))
    ids = [c.id for c in DesktopProvider().commands(ctx=None)]
    assert not any(i.startswith("window.") for i in ids), ids


@pytest.mark.asyncio
async def test_desktop_detect_true_on_plasma(monkeypatch):
    monkeypatch.setattr(
        "gabagent.commands.providers.desktop.shutil.which",
        _only("plasmashell", "kwin_wayland", "qdbus6", "kscreen-doctor"),
    )
    assert await DesktopProvider().detect(ctx=None) is True
    ids = [c.id for c in DesktopProvider().commands(ctx=None)]
    assert "window.to_largest_screen" in ids  # host behavior preserved


# ---- Fork E: media honesty gate ---------------------------------------------

def _cat(domains):
    return types.SimpleNamespace(domains=lambda: list(domains))


def test_has_media_true_when_catalog_has_media_domain():
    ctx = types.SimpleNamespace(command_catalog=_cat(["media", "timer"]), config=None, room_id="laptop")
    assert _has_media_capability(ctx) is True


def test_has_media_false_when_no_media_domain_and_no_cast_target():
    cfg = GabAgentConfig(api_key="test")
    ctx = types.SimpleNamespace(command_catalog=_cat(["timer", "window"]), config=cfg, room_id="laptop")
    assert _has_media_capability(ctx) is False


def test_has_media_true_for_cast_target_room_without_local_player():
    # A control room with no local media provider but a configured Jellyfin cast client is NOT muzzled.
    cfg = GabAgentConfig(api_key="test", room_media={"satellite": {"jellyfin_client_target": "satellite-jellyfin"}})
    ctx = types.SimpleNamespace(command_catalog=None, config=cfg, room_id="satellite")
    assert _has_media_capability(ctx) is True


def test_has_media_false_when_catalog_none_and_no_room_media():
    cfg = GabAgentConfig(api_key="test")
    ctx = types.SimpleNamespace(command_catalog=None, config=cfg, room_id="laptop")
    assert _has_media_capability(ctx) is False


def _voice_ctx(tmp_path, **cfg_kw):
    cfg = GabAgentConfig(api_key="test", **cfg_kw)
    cfg.router.enabled = False
    ctx = AgentContext(
        config=cfg, client=None,
        rate_limiter=types.SimpleNamespace(record=lambda *a, **k: None),
        session=None, session_id="s", cwd=tmp_path, system_prompt="", headless=True,
    )
    return ctx


def test_voice_system_injects_honest_line_when_no_media(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    ctx = _voice_ctx(tmp_path)        # no catalog, no room_media → media-less device
    ctx.room_id = "laptop"
    assert _HONEST in _voice_system(ctx)


def test_voice_system_no_line_for_cast_target_room(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    ctx = _voice_ctx(tmp_path, room_media={"satellite": {"jellyfin_client_target": "satellite-jellyfin"}})
    ctx.room_id = "satellite"
    assert _HONEST not in _voice_system(ctx)
