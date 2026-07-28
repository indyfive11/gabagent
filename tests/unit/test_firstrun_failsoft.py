"""First-run fail-soft gate (Installer Parity SOP — the BEHAVIOR the packaging superset check is blind to).

Gate 1 proves an optdepend is *declared* in the PKGBUILD; it CANNOT prove that a code path which needs an
optional package degrades gracefully when that package is absent. `anthropic` and `playwright` are pyproject
CORE deps demoted to PKGBUILD *optdepends* — pacman does not auto-install optdepends, so a default AUR
install runs without them. A code path that imports one unconditionally raw-crashes with ModuleNotFoundError
(the exact class of the voice-agent dotenv first-run crash). These tests pin the fail-soft contract:

  - `anthropic_configured()` is importability-aware, so the escalation ladder omits a claude rung it can't
    build instead of selecting it and crashing (was a key-only check).
  - `build_client_for("claude")` returns None when the package is absent (ladder falls through).
  - `degrade_provider_if_unavailable()` falls a claude-configured runtime back to gab (startup stays usable).

The raw-import guards themselves (`_load_claudette`, the browser command) are proven against a REAL absent
package by scripts/firstrun_smoke.sh — an in-tree test cannot uninstall a dep it is running under.
"""
from __future__ import annotations

import gabagent.api.factory as factory
from gabagent.api.rate_limit import UsageTracker
from gabagent.config.loader import load_config


def _cfg_claude_with_key():
    cfg = load_config({})
    cfg.provider = "claude"
    cfg.claude.api_key = "sk-test-key"
    return cfg


def test_anthropic_configured_is_importability_aware(monkeypatch):
    """Condition C: a key alone is NOT enough — the package must be importable, else the ladder selects a
    claude rung it can't construct. With the package 'absent', configured must be False despite the key."""
    cfg = _cfg_claude_with_key()
    monkeypatch.setattr(factory, "anthropic_available", lambda: False)
    assert factory.anthropic_configured(cfg) is False
    monkeypatch.setattr(factory, "anthropic_available", lambda: True)
    assert factory.anthropic_configured(cfg) is True


def test_ladder_omits_claude_when_package_absent(monkeypatch):
    """build_client_for('claude') returns None (rung omitted, ladder falls through) when anthropic is
    absent — never a raw import crash."""
    cfg = _cfg_claude_with_key()
    monkeypatch.setattr(factory, "anthropic_available", lambda: False)
    client = factory.build_client_for("claude", cfg, UsageTracker(simple_model="x"))
    assert client is None


def test_startup_degrades_claude_to_gab_when_package_absent(monkeypatch):
    """A saved provider=claude on a box without the 'anthropic' package degrades to gab (returns True) so
    startup stays usable instead of crashing at build_client."""
    cfg = _cfg_claude_with_key()
    monkeypatch.setattr(factory, "anthropic_available", lambda: False)
    assert factory.degrade_provider_if_unavailable(cfg) is True
    assert cfg.provider == "gab"


def test_no_degrade_when_package_present(monkeypatch):
    """When the package is present, a claude config is left untouched (no spurious fallback)."""
    cfg = _cfg_claude_with_key()
    monkeypatch.setattr(factory, "anthropic_available", lambda: True)
    assert factory.degrade_provider_if_unavailable(cfg) is False
    assert cfg.provider == "claude"
