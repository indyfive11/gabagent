"""Layer-A hardware detection — the NET-NEW GPU vendor-triple detector (A.3)."""
from __future__ import annotations

from installkit import hardware


def _patch(monkeypatch, which_present, outputs):
    """which_present: set of tool names present. outputs: {argv[0]: stdout} for _run()."""
    monkeypatch.setattr(
        hardware.shutil, "which",
        lambda c: ("/usr/bin/" + c) if c in which_present else None,
    )
    monkeypatch.setattr(hardware, "_run", lambda argv, timeout=10.0: outputs.get(argv[0], ""))


def test_nvidia_via_smi(monkeypatch):
    _patch(monkeypatch, {"nvidia-smi"}, {"nvidia-smi": "GPU 0: NVIDIA GeForce RTX 4090 (UUID: GPU-x)"})
    g = hardware.detect_gpu()
    assert g.vendor == "nvidia"
    assert g.source == "nvidia-smi"
    assert g.amd_override == ""


def test_nvidia_smi_present_but_empty_falls_through(monkeypatch):
    # driver stub with no GPUs listed → don't claim nvidia; fall through to lspci.
    _patch(monkeypatch, {"nvidia-smi"}, {"nvidia-smi": "\n", "lspci": ""})
    assert hardware.detect_gpu().vendor == "none"


def test_amd_via_rocminfo_with_override(monkeypatch):
    _patch(monkeypatch, {"rocminfo"}, {"rocminfo": "  Name:  gfx1100\n  Marketing Name: Radeon"})
    g = hardware.detect_gpu()
    assert g.vendor == "amd"
    assert g.amd_override == "11.0.0"
    assert g.source == "rocminfo"


def test_amd_override_derivations():
    assert hardware.amd_hsa_override("gfx1030") == "10.3.0"
    assert hardware.amd_hsa_override("gfx900") == "9.0.0"
    assert hardware.amd_hsa_override("gfx1103") == "11.0.0"
    assert hardware.amd_hsa_override("no gfx here") == ""


def test_lspci_fallback_nvidia(monkeypatch):
    _patch(monkeypatch, set(), {"lspci": "01:00.0 VGA compatible controller: NVIDIA Corporation GA102"})
    g = hardware.detect_gpu()
    assert g.vendor == "nvidia"
    assert g.source == "lspci"


def test_lspci_fallback_amd(monkeypatch):
    _patch(monkeypatch, set(), {"lspci": "03:00.0 VGA compatible controller: Advanced Micro Devices [AMD/ATI]"})
    g = hardware.detect_gpu()
    assert g.vendor == "amd"
    assert g.source == "lspci"
    assert g.amd_override == ""  # lspci can't give the gfx target


def test_lspci_ignores_non_display_lines(monkeypatch):
    _patch(monkeypatch, set(), {"lspci": "00:1f.3 Audio device: NVIDIA Corporation HD Audio"})
    assert hardware.detect_gpu().vendor == "none"


def test_no_gpu_returns_none(monkeypatch):
    _patch(monkeypatch, set(), {})
    g = hardware.detect_gpu()
    assert g.vendor == "none"
    assert g.source == "none"


def test_detect_gpu_never_raises_when_probes_throw(monkeypatch):
    # Portability SOP: even if the underlying subprocess blows up, _run swallows it and detect_gpu
    # degrades to 'none' — a probe failure must never propagate out of detection.
    monkeypatch.setattr(hardware.shutil, "which", lambda c: "/usr/bin/" + c)  # all tools "present"

    def boom(*a, **k):
        raise OSError("probe blew up")
    monkeypatch.setattr(hardware.subprocess, "run", boom)  # the REAL _run must catch this
    g = hardware.detect_gpu()
    assert g.vendor == "none"
    assert g.source == "none"
