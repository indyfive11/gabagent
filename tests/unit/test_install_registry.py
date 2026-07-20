"""Phase-2 plugin-installer registry — every registered entry loads and conforms."""
from __future__ import annotations

from gabagent.install.contract import Manifest, PluginInstaller
from gabagent.install.registry import INSTALLERS, load_installers


def test_registry_is_non_empty():
    assert len(INSTALLERS) >= 1


def test_every_entry_conforms_to_the_contract():
    for inst in load_installers():
        assert isinstance(inst.name, str) and inst.name
        assert isinstance(inst.manifest, Manifest)
        assert callable(inst.check) and callable(inst.configure)
        assert isinstance(inst, PluginInstaller)


def test_jellyfin_is_registered():
    names = {i.name for i in load_installers()}
    assert "jellyfin" in names


def test_load_installers_returns_a_fresh_list():
    a = load_installers()
    a.append("mutated")  # type: ignore[arg-type]
    assert "mutated" not in load_installers()  # registry not mutated by a caller
