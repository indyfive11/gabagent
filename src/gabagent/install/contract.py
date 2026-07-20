"""Plugin-installer contract — Layer C (gabagent-only).

Phase-2. A capability plugin describes its install needs via a `Manifest` and implements two methods:
`check()` (dry-run: is it configured / reachable / what's missing) and `configure()` (write THIS plugin's
config into the passed cfg object). Plugins DECLARE; the shared core (installkit.deps) INSTALLS + DEDUPES —
so there is deliberately NO per-plugin `install()`: system-package provisioning is done ONCE at the wizard
level over the UNION of all selected manifests, which per-plugin shell-outs could not dedupe.

This contract lives in Layer C, NOT installkit: it references gabagent config, and the voice-agent side is a
flat linear role-provisioner with no plugin registry, so it must never vendor a contract it doesn't use
(keeps installkit's import-isolation invariant intact).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Manifest:
    """A plugin's declarative dependency list.

    Only `system_pkgs` is HONORED today (resolved per-distro + deduped across plugins by installkit.deps).
    The remaining fields are DECLARED-BUT-NOT-YET-HONORED — their engines land in later phases, so a plugin
    that sets them today gets a silent no-op:
      - aur_pkgs   : no AUR path exists in installkit.deps yet (Arch-only extras).
      - python_deps: the per-installer Python-provisioning callback is not wired yet; and a dep may belong to
                     a *different* interpreter (e.g. mopidy-tidal → mopidy's env, not gabagent's venv).
      - models     : model-artifact prefetch is a voice/STT-phase concern.
      - services   : systemd-unit templating is Phase-3 (voice) work.
    Declare them for forward-compatibility, but do not rely on them being provisioned before their phase.
    """
    system_pkgs: tuple[str, ...] = ()
    aur_pkgs: tuple[str, ...] = ()
    python_deps: tuple[str, ...] = ()
    models: tuple[str, ...] = ()
    services: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckReport:
    """The dry-run capability report `check()` returns. Pure inspection — never mutates, never raises."""
    name: str
    configured: bool                       # is this plugin's config populated (e.g. an api_key is set)?
    reachable: bool | None = None          # live-probe result; None ⇒ not applicable / not probed
    missing_system: tuple[str, ...] = ()
    missing_python: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class PluginInstaller(Protocol):
    """Structural contract every plugin installer descriptor satisfies. Concrete descriptors are small
    stateless objects exposing `name` + `manifest` and the two methods."""
    name: str
    manifest: Manifest

    def check(self, cfg) -> CheckReport:
        """Dry-run: report configured/reachable state + what's missing. Must never raise, never write."""
        ...

    def configure(self, cfg, *, ask) -> bool:
        """Mutate THIS plugin's config sub-model on `cfg` and return True iff something changed. The caller
        (the Layer-C wizard) owns the single `save_config` — configure() never persists. `ask` is an injected
        prompt object exposing `prompt(text, default="")` and `confirm(text, default=True)` (installkit.wizard
        satisfies it structurally), so configure() is UI-primitive-only and unit-testable."""
        ...
