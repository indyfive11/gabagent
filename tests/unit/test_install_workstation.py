"""Phase-1 workstation wizard (Layer C) — orchestration + delegation to the backend picker.

These are synchronous tests: main() owns its own asyncio.run, so we call it from a non-async test.
"""
from __future__ import annotations

import gabagent.config.setup as setup_mod
from gabagent.install import workstation
from installkit import deps, hardware


def _stub_detection(monkeypatch, *, ok=True, missing=None):
    readiness = deps.SystemReadiness(
        distro="arch", pkg_manager="pacman",
        present=["git", "rg"] if ok else ["git"],
        missing=[] if ok else (missing or ["rg"]),
    )
    gpu = hardware.GpuInfo(vendor="amd", amd_override="11.0.0", source="rocminfo")
    monkeypatch.setattr(workstation, "_detect_box", lambda: (readiness, gpu))


def _stub_backend_picker(monkeypatch):
    """Replace the real (interactive) picker with an async spy."""
    called = {"n": 0}

    async def fake_setup(cfg):
        called["n"] += 1
        return cfg
    monkeypatch.setattr(setup_mod, "run_first_time_setup", fake_setup)
    return called


def test_workstation_role_delegates_to_backend_picker(monkeypatch):
    monkeypatch.setattr(workstation.wizard, "choose", lambda *a, **k: 0)  # Workstation
    _stub_detection(monkeypatch, ok=True)
    called = _stub_backend_picker(monkeypatch)

    rc = workstation.main()

    assert rc == 0
    assert called["n"] == 1  # the picker (config write) ran exactly once


def test_non_workstation_role_returns_early_without_writing(monkeypatch):
    monkeypatch.setattr(workstation.wizard, "choose", lambda *a, **k: 1)  # a "later phase" role
    called = _stub_backend_picker(monkeypatch)
    # detection/backend must NOT run for a stub role
    monkeypatch.setattr(workstation, "_detect_box", lambda: (_ for _ in ()).throw(AssertionError("no detect")))

    rc = workstation.main()

    assert rc == 0
    assert called["n"] == 0  # no config write for an unbuilt role


def test_missing_tools_offer_declined_still_configures(monkeypatch):
    monkeypatch.setattr(workstation.wizard, "choose", lambda *a, **k: 0)
    _stub_detection(monkeypatch, ok=False, missing=["rg"])
    monkeypatch.setattr(workstation.wizard, "confirm", lambda *a, **k: False)  # decline the install
    ran_install = {"n": 0}
    monkeypatch.setattr(workstation.deps, "run_install", lambda *a, **k: ran_install.__setitem__("n", 1))
    called = _stub_backend_picker(monkeypatch)

    rc = workstation.main()

    assert rc == 0
    assert ran_install["n"] == 0     # declined → never installed
    assert called["n"] == 1          # but config is still written


def test_missing_tools_offer_accepted_runs_install(monkeypatch):
    monkeypatch.setattr(workstation.wizard, "choose", lambda *a, **k: 0)
    _stub_detection(monkeypatch, ok=False, missing=["rg"])
    monkeypatch.setattr(workstation.wizard, "confirm", lambda *a, **k: True)  # accept
    ran = {"argv": None}
    monkeypatch.setattr(workstation.deps, "run_install", lambda argv, **k: ran.__setitem__("argv", argv) or 0)
    _stub_backend_picker(monkeypatch)

    rc = workstation.main()

    assert rc == 0
    assert ran["argv"] == ["pacman", "-S", "--needed", "ripgrep"]


def test_cancel_during_flow_returns_1(monkeypatch):
    def cancel(*a, **k):
        raise workstation.wizard.WizardCancelled
    monkeypatch.setattr(workstation.wizard, "choose", cancel)

    assert workstation.main() == 1
