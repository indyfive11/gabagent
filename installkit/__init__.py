"""installkit — the genuinely-shared installer core (Layer A).

Born at the Gab-Agent text-only workstation MVP (Phase-1). This package is the ONLY code shared
between the gabagent and voice-agent installers; it is later vendored into the voice-agent tree at a
pinned commit SHA (Phase-3), so it carries two hard invariants:

  1. IMPORT ISOLATION — installkit imports ONLY the Python standard library (plus anything it
     explicitly declares). It must NEVER `import gabagent` / `from gabagent …`; a per-repo layer owns
     all app-specific config knowledge and writes. Enforced by tests/unit/test_installkit_import_isolation.py.
  2. PROVISIONAL INTERFACES — the text MVP exercises only the wizard primitives, the system dep-engine,
     and hardware detection. Unit/env templating (A.4) and token pairing (A.5) are deliberately NOT here
     yet: they are first shaped by voice in Phase-3, which is when this surface stabilizes and the
     vendor-pin activates. Keep additions minimal until then.

What lives here today (the MVP-exercised third of Layer A):
  - wizard   — pure-stdlib interactive primitives (prompt / choose / confirm / save-confirm + panels).
  - deps     — the dependency-engine SYSTEM layer (distro detect, uv presence, system-pkg readiness).
  - hardware — one-shot hardware detection that RETURNS values (never writes config); the GPU probe
               reports the vendor triple `amd | nvidia | none` needed for the STT split.
"""

__all__ = ["wizard", "deps", "hardware"]
__version__ = "0.1.0"
