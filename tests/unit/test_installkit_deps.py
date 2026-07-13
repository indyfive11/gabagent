"""Layer-A dependency-engine SYSTEM layer — distro detect, readiness, install-command shaping."""
from __future__ import annotations

from installkit import deps


def _patch_which(monkeypatch, present):
    """Make deps.have()/package_manager() see only `present` tools as installed."""
    monkeypatch.setattr(deps.shutil, "which", lambda cmd: ("/usr/bin/" + cmd) if cmd in present else None)


def _patch_os_release(monkeypatch, tmp_path, contents):
    f = tmp_path / "os-release"
    f.write_text(contents)
    real_open = open

    def fake_open(path, *a, **k):
        if path == "/etc/os-release":
            return real_open(f, *a, **k)
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", fake_open)


def test_detect_distro_arch(monkeypatch, tmp_path):
    _patch_os_release(monkeypatch, tmp_path, 'ID=cachyos\nID_LIKE=arch\n')
    assert deps.detect_distro() == "arch"


def test_detect_distro_debian_via_id_like(monkeypatch, tmp_path):
    _patch_os_release(monkeypatch, tmp_path, 'ID=raspbian\nID_LIKE=debian\n')
    assert deps.detect_distro() == "debian"


def test_detect_distro_ubuntu(monkeypatch, tmp_path):
    _patch_os_release(monkeypatch, tmp_path, 'ID=ubuntu\n')
    assert deps.detect_distro() == "debian"


def test_detect_distro_unknown_returns_none(monkeypatch, tmp_path):
    _patch_os_release(monkeypatch, tmp_path, 'ID=plan9\n')
    assert deps.detect_distro() is None


def test_detect_distro_missing_file_returns_none(monkeypatch):
    def boom(path, *a, **k):
        raise OSError
    monkeypatch.setattr("builtins.open", boom)
    assert deps.detect_distro() is None


def test_package_manager_arch(monkeypatch):
    _patch_which(monkeypatch, {"pacman"})
    assert deps.package_manager("arch") == "pacman"


def test_package_manager_absent_binary_returns_none(monkeypatch):
    _patch_which(monkeypatch, set())
    assert deps.package_manager("debian") is None


def test_install_command_maps_rg_to_ripgrep_debian():
    cmd = deps.install_command(["git", "rg"], distro="debian")
    assert cmd == ["apt", "install", "-y", "git", "ripgrep"]


def test_install_command_arch_needed_flag():
    cmd = deps.install_command(["rg"], distro="arch")
    assert cmd == ["pacman", "-S", "--needed", "ripgrep"]


def test_install_command_unknown_distro_none():
    # An explicitly-unknown distro family yields no command (distro=None would auto-detect the host).
    assert deps.install_command(["git"], distro="plan9") is None


def test_install_command_empty_tools_none():
    assert deps.install_command([], distro="arch") is None


def test_check_system_readiness_all_present(monkeypatch):
    _patch_which(monkeypatch, {"git", "rg", "pacman"})
    r = deps.check_system_readiness(["git", "rg"], distro="arch")
    assert r.ok is True
    assert r.missing == []
    assert set(r.present) == {"git", "rg"}
    assert r.suggested_install() is None


def test_check_system_readiness_missing(monkeypatch):
    _patch_which(monkeypatch, {"git", "apt"})
    r = deps.check_system_readiness(["git", "rg"], distro="debian")
    assert r.ok is False
    assert r.missing == ["rg"]
    assert r.suggested_install() == ["apt", "install", "-y", "ripgrep"]


def test_readiness_default_tools_are_workstation(monkeypatch):
    _patch_which(monkeypatch, set())
    r = deps.check_system_readiness(distro="arch")
    assert r.missing == deps.WORKSTATION_TOOLS


def test_extra_names_override_merges_over_shared_table():
    # VAC's Layer-B case: portaudio differs per family and must NOT pollute the shared _PKG_NAMES.
    extra = {"portaudio": {"debian": "portaudio19-dev", "arch": "portaudio"}}
    cmd = deps.install_command(["portaudio", "rg"], distro="debian", extra_names=extra)
    assert cmd == ["apt", "install", "-y", "portaudio19-dev", "ripgrep"]  # extra + shared both resolve


def test_extra_names_does_not_mutate_shared_table():
    before = dict(deps._PKG_NAMES)
    deps.install_command(["portaudio"], distro="arch", extra_names={"portaudio": {"arch": "portaudio"}})
    assert deps._PKG_NAMES == before  # shared table stays app-agnostic


def test_extra_names_unset_is_mvp_behavior():
    assert deps.install_command(["rg"], distro="debian") == ["apt", "install", "-y", "ripgrep"]


def test_readiness_carries_extra_names_into_suggested_install(monkeypatch):
    _patch_which(monkeypatch, {"apt"})  # everything else missing
    extra = {"portaudio": {"debian": "portaudio19-dev"}}
    r = deps.check_system_readiness(["portaudio"], distro="debian", extra_names=extra)
    assert r.missing == ["portaudio"]
    assert r.suggested_install() == ["apt", "install", "-y", "portaudio19-dev"]


def test_uv_present(monkeypatch):
    _patch_which(monkeypatch, {"uv"})
    assert deps.uv_present() is True
    _patch_which(monkeypatch, set())
    assert deps.uv_present() is False
