import types
import pytest

from gabagent.commands.providers.mpris import PROVIDER as mpris
from gabagent.commands.providers.system import PROVIDER as system
from gabagent.commands.providers.applaunch import PROVIDER as applaunch


def _which(present):
    return lambda b: ("/usr/bin/" + b) if b in present else None


def _ctx():
    return types.SimpleNamespace(config=types.SimpleNamespace())


async def test_mpris_detect_and_tiers(monkeypatch):
    monkeypatch.setattr("shutil.which", _which({"playerctl"}))
    assert await mpris.detect(_ctx()) is True
    cmds = {c.id: c for c in mpris.commands(_ctx())}
    assert cmds["media.playpause"].tier == 1 and cmds["media.next"].tier == 1
    assert cmds["media.playpause"].backend.argv == ["playerctl", "play-pause"]


async def test_mpris_absent(monkeypatch):
    monkeypatch.setattr("shutil.which", _which(set()))
    assert await mpris.detect(_ctx()) is False


async def test_system_volume_tier1_power_tier3(monkeypatch):
    monkeypatch.setattr("shutil.which", _which({"pactl", "systemctl"}))
    assert await system.detect(_ctx()) is True
    cmds = {c.id: c for c in system.commands(_ctx())}
    assert cmds["system.volume_up"].tier == 1
    assert cmds["system.mute"].backend.argv[0] == "pactl"
    assert cmds["system.poweroff"].tier == 3 and cmds["system.suspend"].tier == 3
    assert "system.brightness_up" not in cmds  # brightnessctl absent


async def test_system_wpctl_fallback(monkeypatch):
    monkeypatch.setattr("shutil.which", _which({"wpctl"}))
    cmds = {c.id: c for c in system.commands(_ctx())}
    assert cmds["system.volume_up"].backend.argv[0] == "wpctl"
    assert "system.poweroff" not in cmds  # systemctl absent


async def test_applaunch(monkeypatch):
    monkeypatch.setattr("shutil.which", _which({"gtk-launch", "xdg-open"}))
    assert await applaunch.detect(_ctx()) is True
    cmds = {c.id: c for c in applaunch.commands(_ctx())}
    assert cmds["app.launch"].tier == 1   # opening an app is harmless/reversible
    assert cmds["app.launch"].backend.argv == ["gtk-launch", "{app}"]
    assert cmds["app.open_url"].tier == 1
    # the {app} slot is a single argv token — a value can't inject a second command
    assert cmds["app.launch"].params[0].name == "app"
