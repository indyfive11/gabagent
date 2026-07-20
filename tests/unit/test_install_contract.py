"""Phase-2 plugin-installer contract — shape + conformance."""
from __future__ import annotations

import dataclasses

import pytest

from gabagent.install.contract import CheckReport, Manifest, PluginInstaller


def test_manifest_defaults_all_empty():
    m = Manifest()
    assert m.system_pkgs == ()
    assert m.aur_pkgs == ()
    assert m.python_deps == ()
    assert m.models == ()
    assert m.services == ()


def test_manifest_is_frozen():
    m = Manifest(system_pkgs=("git",))
    with pytest.raises(dataclasses.FrozenInstanceError):
        m.system_pkgs = ("rg",)  # type: ignore[misc]


def test_checkreport_minimal():
    r = CheckReport(name="x", configured=False)
    assert r.reachable is None
    assert r.missing_system == () and r.missing_python == () and r.notes == ()


def test_runtime_checkable_protocol_matches_a_conforming_object():
    class Good:
        name = "good"
        manifest = Manifest()

        def check(self, cfg):
            return CheckReport(name="good", configured=False)

        def configure(self, cfg, *, ask):
            return False

    assert isinstance(Good(), PluginInstaller)
