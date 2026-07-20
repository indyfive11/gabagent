"""Phase-2 engine path — manifest union/dedup → install argv, plus the run_install elevate/error path.

The reference plugin (Jellyfin) has an empty manifest, so this fixture exercises the system_pkgs path with
synthetic manifests — WITHOUT mutating a real box. It also asserts installkit.deps.run_install's
`["sudo"]+argv` elevate-prepend and its `except → return 1` path, which had NO prior test coverage."""
from __future__ import annotations

from installkit import deps

from gabagent.install.aggregate import dedupe_system_pkgs, plan_system_install
from gabagent.install.contract import Manifest


def test_dedupe_unions_and_is_order_stable():
    a = Manifest(system_pkgs=("git", "rg"))
    b = Manifest(system_pkgs=("rg", "curl"))
    assert dedupe_system_pkgs([a, b]) == ["git", "rg", "curl"]  # first-seen order, deduped


def test_dedupe_empty():
    assert dedupe_system_pkgs([Manifest(), Manifest()]) == []


def test_plan_none_when_nothing_to_install():
    assert plan_system_install([Manifest(), Manifest()], distro="arch") is None


def test_plan_builds_deduped_argv_on_arch():
    a = Manifest(system_pkgs=("git", "curl"))
    b = Manifest(system_pkgs=("curl", "htop"))
    argv = plan_system_install([a, b], distro="arch")
    assert argv == ["pacman", "-S", "--needed", "git", "curl", "htop"]


def test_plan_resolves_per_distro_package_names():
    # `rg` → `ripgrep` on arch via installkit.deps' name table; the aggregate must not fight that mapping.
    argv = plan_system_install([Manifest(system_pkgs=("rg",))], distro="arch")
    assert argv == ["pacman", "-S", "--needed", "ripgrep"]


def test_plan_unknown_distro_is_none():
    assert plan_system_install([Manifest(system_pkgs=("git",))], distro="plan9") is None


def test_run_install_prepends_sudo_when_not_root(monkeypatch):
    calls = {}

    class _Done:
        returncode = 0

    def fake_run(cmd, *a, **k):
        calls["cmd"] = cmd
        return _Done()

    monkeypatch.setattr(deps, "_is_root", lambda: False)
    monkeypatch.setattr(deps.subprocess, "run", fake_run)
    rc = deps.run_install(["pacman", "-S", "--needed", "git"], elevate=True)
    assert rc == 0
    assert calls["cmd"] == ["sudo", "pacman", "-S", "--needed", "git"]


def test_run_install_no_sudo_when_root(monkeypatch):
    calls = {}

    class _Done:
        returncode = 0

    def fake_run(cmd, *a, **k):
        calls["cmd"] = cmd
        return _Done()

    monkeypatch.setattr(deps, "_is_root", lambda: True)
    monkeypatch.setattr(deps.subprocess, "run", fake_run)
    deps.run_install(["apt", "install", "-y", "git"], elevate=True)
    assert calls["cmd"] == ["apt", "install", "-y", "git"]  # no sudo prepended


def test_run_install_returns_1_on_oserror(monkeypatch):
    def boom(cmd, *a, **k):
        raise OSError("no such binary")

    monkeypatch.setattr(deps, "_is_root", lambda: False)
    monkeypatch.setattr(deps.subprocess, "run", boom)
    assert deps.run_install(["pacman", "-S", "git"]) == 1
