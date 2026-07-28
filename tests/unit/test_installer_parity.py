"""Installer-parity gates (see the Installer Parity HARD SOP in CLAUDE.md).

The installer must never lag the code. These are the AUTOMATIC forcing-functions — they run in the suite
and CI, so a parity drift fails the build with no discipline required. Three gates:

  Gate 1  — packaging parity: every runtime/optional dep in pyproject (∪ declared non-python system_pkgs)
            is covered by the AUR PKGBUILD depends/optdepends. Closes the v0.8.0-class "new dep shipped
            without a PKGBUILD entry" miss. Cross-repo (PKGBUILD lives in ~/dev/gabagent-aur), so it
            SKIPS LOUD when the clone is absent and is a HARD release-gate at AUR-bump time.
  Gate 1b — uv.lock parity: `uv lock --check` — the lockfile is in sync with pyproject.
  Gate 2  — plugin-registry conformance: every registered plugin installer satisfies the contract and its
            declared system_pkgs are well-formed + resolvable by installkit.deps for each known distro.

Reachability (does a fresh install actually REACH the feature) is NOT proven here — an in-tree check proves
the artifact was edited, not reached. That is the scripted install smoke's job; see the SOP.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from gabagent.install.registry import load_installers
from gabagent.install.contract import PluginInstaller
from installkit import deps

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
AUR_PKGBUILD = Path.home() / "dev" / "gabagent-aur" / "PKGBUILD"
KNOWN_DISTROS = ("arch", "debian", "fedora")


def _norm(pkg: str) -> str:
    """Canonicalise a package name for comparison: lowercase, unify '_' and '-' (Arch uses
    'python-prompt_toolkit' while PyPI uses 'prompt-toolkit')."""
    return pkg.strip().lower().replace("_", "-")


def _pyproject() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _base_name(spec: str) -> str:
    """'openai>=1.55' -> 'openai'; 'foo[extra]>=1' -> 'foo'."""
    return re.split(r"[<>=!~ \[;]", spec.strip(), maxsplit=1)[0]


def _expected_pkgbuild_names() -> set[str]:
    """The set of PKGBUILD package names that MUST appear (in depends or optdepends), derived from
    pyproject: every core dep + every `voice` optional dep becomes `python-<name>`, unioned with the
    declared non-python `[tool.gabagent.install].system_pkgs`."""
    pp = _pyproject()
    proj = pp["project"]
    py_deps = list(proj.get("dependencies", []))
    py_deps += list(proj.get("optional-dependencies", {}).get("voice", []))
    expected = {_norm(f"python-{_base_name(d)}") for d in py_deps}
    declared = pp.get("tool", {}).get("gabagent", {}).get("install", {}).get("system_pkgs", [])
    expected |= {_norm(p) for p in declared}
    return expected


def _provided_names_from_text(text: str) -> set[str]:
    """Parse a PKGBUILD's `depends=(...)` and `optdepends=(...)` arrays into a normalised name set.
    optdepends entries are 'pkgname: description' — take the token before the colon.

    A parser that can't parse must FAIL, not guess (ledger-171 class; cross-flagged by VAC): a malformed
    array — an opener with no closing ')', so the non-greedy capture over-runs across newlines into a LATER
    array — must raise, never silently over-capture and pass on the inflated blob."""
    names: set[str] = set()
    for field in ("depends", "optdepends"):
        if not re.search(rf"^{field}=\(", text, re.MULTILINE):
            continue  # field genuinely absent (optdepends is optional) — fine
        # Close on a ')' at the START of a line — an optdepends description may itself contain ')'
        # (e.g. 'python-anthropic: Claude/Anthropic backend (/backend claude)'), which a naive
        # non-greedy '.*?)' would stop at, silently truncating the array.
        m = re.search(rf"^{field}=\((.*?)^\)", text, re.DOTALL | re.MULTILINE)
        if not m:
            raise ValueError(f"PKGBUILD {field}=() array has no closing ')': refusing to guess — fix the PKGBUILD.")
        body = m.group(1)
        # A well-formed array body never contains another `word=(` opener. If it does, this array's ')'
        # was missing and the capture ran into a later array — malformed, fail loud.
        if re.search(r"^\w+=\(", body, re.MULTILINE):
            raise ValueError(
                f"PKGBUILD {field}=() over-captured into a later array (missing ')'): refusing to guess — "
                "fix the PKGBUILD."
            )
        for raw in re.findall(r"""['"]([^'"]+)['"]""", body):
            token = raw.split(":", 1)[0].strip()
            # strip any version constraint on a depends entry (e.g. 'python>=3.12')
            token = re.split(r"[<>=]", token, maxsplit=1)[0]
            if token:
                names.add(_norm(token))
    return names


def _pkgbuild_provided_names() -> set[str]:
    """Gate-1 helper: the normalised name set the AUR PKGBUILD provides (depends ∪ optdepends)."""
    return _provided_names_from_text(AUR_PKGBUILD.read_text())


def test_pkgbuild_parser_fails_loud_on_malformed():
    """Gate-1 parser robustness — a parser that can't parse must FAIL, not guess (ledger-171 class; VAC
    cross-flag). A depends array missing its closing ')' must raise, not silently over-capture the following
    optdepends array (which would inflate the provided set and pass a malformed PKGBUILD)."""
    well_formed = "depends=(\n  'python'\n)\noptdepends=(\n  'python-x: y'\n)\n"
    assert _provided_names_from_text(well_formed) == {"python", "python-x"}

    missing_close_runs_into_next = "depends=(\n  'python'\noptdepends=(\n  'python-x: y'\n)\n"
    with pytest.raises(ValueError):
        _provided_names_from_text(missing_close_runs_into_next)

    last_array_unclosed = "depends=(\n  'python'\n)\noptdepends=(\n  'python-x: y'\n"
    with pytest.raises(ValueError):
        _provided_names_from_text(last_array_unclosed)


@pytest.mark.skipif(
    not AUR_PKGBUILD.exists(),
    reason=f"AUR clone absent ({AUR_PKGBUILD}); packaging parity is a HARD release-gate at AUR-bump time "
    "(see the Installer Parity SOP) — not enforceable in-tree without the clone.",
)
def test_pkgbuild_covers_every_pyproject_dep():
    """Gate 1 — every pyproject core+voice dep (and declared system_pkg) is present in the PKGBUILD
    depends/optdepends. Direction is subset: the PKGBUILD may carry EXTRA optdepends (e.g. a backend the
    core doesn't require) — that is fine; a pyproject dep that is MISSING from packaging is the regression
    (the v0.8.0 zeroconf-optdepends miss)."""
    expected = _expected_pkgbuild_names()
    provided = _pkgbuild_provided_names()
    missing = sorted(expected - provided)
    assert not missing, (
        f"PKGBUILD is missing packaging for pyproject deps: {missing}. "
        f"Add them to depends/optdepends in ~/dev/gabagent-aur/PKGBUILD (python-<name>)."
    )


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_uv_lock_in_sync_with_pyproject():
    """Gate 1b — the lockfile is in sync with pyproject. A new/bumped dep in pyproject that was not
    re-locked drifts the lock; `uv lock --check` catches it hermetically."""
    proc = subprocess.run(
        ["uv", "lock", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        "uv.lock is out of sync with pyproject.toml — run `uv lock` and commit the result.\n"
        f"{proc.stderr or proc.stdout}"
    )


def test_plugin_registry_conformance():
    """Gate 2 — every registered plugin installer satisfies the PluginInstaller contract, and each
    declared system_pkg is a well-formed name that installkit.deps can turn into an install command for
    every known distro (catches a malformed descriptor or an empty/whitespace package token). This does
    NOT prove the plugin needs or is satisfied by those pkgs — reachability is the install-smoke's job."""
    installers = load_installers()
    assert installers, "plugin-installer registry is empty — expected at least the Jellyfin reference plugin"
    for inst in installers:
        assert isinstance(inst, PluginInstaller), f"{inst!r} does not satisfy the PluginInstaller contract"
        for pkg in inst.manifest.system_pkgs:
            assert isinstance(pkg, str) and pkg.strip(), f"{inst.name}: bad system_pkg {pkg!r}"
            for distro in KNOWN_DISTROS:
                argv = deps.install_command([pkg], distro)
                assert argv and pkg.split(">")[0].split("=")[0] not in ("", " "), (
                    f"{inst.name}: system_pkg {pkg!r} did not resolve to an install argv on {distro}"
                )
