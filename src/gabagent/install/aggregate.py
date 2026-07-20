"""System-package aggregation — Layer C.

The declare/execute split: plugins DECLARE their `system_pkgs` in their Manifest; this module UNIONS +
dedupes them across all selected plugins and hands a single flat list to the existing installkit.deps engine
(`install_command` already takes a `list[str]`). Doing the union here — not inside installkit — is why Phase-2
changes zero bytes of installkit: the shared core stays a dumb per-list engine, the app-specific "which
plugins are selected" logic stays in Layer C.
"""
from __future__ import annotations

from collections.abc import Iterable

from installkit import deps

from gabagent.install.contract import Manifest


def dedupe_system_pkgs(manifests: Iterable[Manifest]) -> list[str]:
    """Union the `system_pkgs` of several manifests into one order-stable, de-duplicated list.

    Order-stable (first-seen wins) so the emitted install command is deterministic across runs — sorted would
    also work, but first-seen keeps a plugin author's intended grouping. An empty union returns []."""
    seen: dict[str, None] = {}
    for m in manifests:
        for pkg in m.system_pkgs:
            seen.setdefault(pkg, None)
    return list(seen)


def plan_system_install(manifests: Iterable[Manifest], distro: str | None = None) -> list[str] | None:
    """Build the (sudo-free) argv that would install the deduped union of every manifest's system_pkgs on
    this distro, or None if there's nothing to install or the distro is unknown. The CALLER decides whether
    to run it (and how to elevate) via `installkit.deps.run_install` — never an implicit install here."""
    pkgs = dedupe_system_pkgs(manifests)
    if not pkgs:
        return None
    return deps.install_command(pkgs, distro)
