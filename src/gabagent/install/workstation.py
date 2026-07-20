"""Phase-1 MVP — the Gab-Agent text-only workstation wizard.

Flow:  role menu → detect box → readiness check → backend picker → summary.
It orchestrates installkit (Layer A) primitives + detection and DELEGATES the actual config write to the
existing first-run backend picker (`config.setup.run_first_time_setup`) — the MVP is ~90% a re-package of
that proven flow behind an explicit `gabagent-install` entry, plus box detection and a readiness gate.

Scope discipline (Phase-1): text-only. No voice, no systemd units, no token pairing, no plugin installers
— those are Phase-2/3. The shared installkit interfaces this exercises are PROVISIONAL until voice forces
their real shape.
"""
from __future__ import annotations

import asyncio
import sys

from installkit import deps, hardware, wizard

# Roles the full installer will offer. Only the workstation is built in Phase-1; the rest are shown as
# stubs so the menu never implies capability that isn't there.
_ROLES = [
    "Workstation — Gab-Agent CLI/TUI, text-only, pick your AI backend",
    "Full voice host — brain + voice + media  (later phase)",
    "Satellite — voice endpoint attached to a LAN brain  (later phase)",
    "Laptop — Gab-Agent + optional voice  (later phase)",
]


def _detect_box() -> tuple[deps.SystemReadiness, hardware.GpuInfo]:
    """One-shot box detection (RETURNS values; nothing is written here — portability SOP)."""
    readiness = deps.check_system_readiness()
    gpu = hardware.detect_gpu()
    return readiness, gpu


def _show_box(readiness: deps.SystemReadiness, gpu: hardware.GpuInfo) -> None:
    wizard.heading("Detected this box:")
    distro = readiness.distro or "unknown"
    pm = readiness.pkg_manager or "—"
    wizard.note(f"  distro family : {distro}   (package manager: {pm})")
    gpu_line = gpu.vendor
    if gpu.vendor == "amd" and gpu.amd_override:
        gpu_line += f"   (suggested HSA_OVERRIDE_GFX_VERSION={gpu.amd_override})"
    wizard.note(f"  GPU vendor    : {gpu_line}   [{gpu.source}]")
    present = ", ".join(readiness.present) or "none"
    wizard.note(f"  tools present : {present}")


def _handle_readiness(readiness: deps.SystemReadiness) -> None:
    """Report missing system tools and offer to install them. Non-fatal: the user can decline and do it
    by hand (the wizard still writes config so a subsequent manual install completes the setup)."""
    if readiness.ok:
        return
    missing = ", ".join(readiness.missing)
    wizard.heading(f"Missing system tools: {missing}")
    cmd = readiness.suggested_install()
    if not cmd:
        wizard.note(
            "  Couldn't map these to a package manager on this distro — please install them manually "
            "(ripgrep provides `rg`, which the agent needs)."
        )
        return
    printable = " ".join(cmd)
    wizard.note(f"  Install command: sudo {printable}")
    if wizard.confirm("  Run it now?", default=False):
        rc = deps.run_install(cmd, elevate=True)
        if rc == 0:
            wizard.note("  ✓ installed.")
        else:
            wizard.note(f"  ✗ install exited {rc} — install manually and re-run if needed.")
    else:
        wizard.note("  Skipped — install manually when convenient; config is still written below.")


async def _run() -> int:
    wizard.panel("Gab-Agent installer", "Phase-1 · text-only workstation")

    idx = wizard.choose("Choose a role to install:", _ROLES, default_index=0)
    if idx != 0:
        wizard.heading("That role isn't built yet.")
        wizard.note(
            "  Phase-1 ships the text-only workstation only. Voice-host / satellite / laptop roles land "
            "in a later phase. Re-run and pick 'Workstation' to continue."
        )
        return 0

    readiness, gpu = _detect_box()
    _show_box(readiness, gpu)
    _handle_readiness(readiness)

    # Delegate the config write to the existing, proven backend picker. This is the per-repo layer that
    # owns gabagent's config schema — installkit never touches settings.json (import-isolation invariant).
    from gabagent.config.loader import load_config
    from gabagent.config.setup import run_first_time_setup

    wizard.heading("Configure your AI backend:")
    cfg = load_config()
    await run_first_time_setup(cfg)  # prints its own confirmation + writes settings.json

    wizard.heading("Workstation ready.")
    wizard.note("  Run `gab` for the interactive REPL, or `gab \"...\"` for a one-shot.")
    return 0


def _show_check(inst, report) -> None:
    """One-line status for a plugin's check() report."""
    cfg_s = "configured" if report.configured else "not configured"
    reach_s = "" if report.reachable is None else (", reachable" if report.reachable else ", unreachable")
    wizard.note(f"  • {inst.name}: {cfg_s}{reach_s}")
    for n in report.notes:
        wizard.note(f"      {n}")


def _run_addons() -> int:
    """Optional add-on configuration (`gabagent-install --addons`). Deliberately NOT part of the default
    workstation flow — the MVP path stays byte-identical. Iterates the plugin-installer registry, shows each
    plugin's check() state, and offers to configure it. The wizard owns the single save_config."""
    wizard.panel("Gab-Agent installer", "optional add-ons")
    from gabagent.config.loader import load_config, save_config
    from gabagent.install.registry import load_installers

    installers = load_installers()
    if not installers:
        wizard.note("  No add-on installers are registered.")
        return 0

    cfg = load_config()
    wizard.heading("Add-on capabilities:")
    changed = False
    for inst in installers:
        report = inst.check(cfg)
        _show_check(inst, report)
        if wizard.confirm(f"  Configure {inst.name}?", default=not report.configured):
            if inst.configure(cfg, ask=wizard):
                changed = True
                wizard.note(f"  ✓ {inst.name} configured.")
            else:
                wizard.note(f"  {inst.name} unchanged.")

    if changed:
        save_config(cfg)
        wizard.heading("Add-ons saved.")
    else:
        wizard.heading("No add-on changes.")
    return 0


def main() -> int:
    """Console-script entry (`gabagent-install`). Synchronous wrapper around the async wizard.
    `--addons` runs the optional plugin-configuration flow instead of the workstation wizard."""
    try:
        if "--addons" in sys.argv[1:]:
            return _run_addons()
        return asyncio.run(_run())
    except wizard.WizardCancelled:
        print("\nSetup cancelled — nothing was written.")
        return 1
    except KeyboardInterrupt:
        print("\nSetup cancelled.")
        return 1
