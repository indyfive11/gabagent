"""gabagent-install — the Gab-Agent-specific installer (Layer C).

This is the app side of the installer: it may freely `import gabagent…` and `import installkit`. It owns
gabagent's own config write (through the existing `config.setup` backend picker) and, in later phases, the
plugin-discovered addon installers. installkit (Layer A) stays app-agnostic; the coupling lives here.

Phase-1 ships ONE role — the text-only workstation (`install.workstation`). Other roles print a
"later phase" stub so the role menu is honest about what's built.
"""
