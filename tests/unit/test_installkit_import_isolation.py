"""INVARIANT (INSTALL_PLAN §4): installkit imports ONLY the stdlib — never `gabagent`.

installkit is vendored into voice-agent at a pinned SHA; a stray `from gabagent…` would make it
un-vendorable and break repo separation. This test IS the enforcement grep the plan calls for.
"""
from __future__ import annotations

import ast
from pathlib import Path

import installkit

_PKG_DIR = Path(installkit.__file__).parent
# Everything installkit is allowed to import: the stdlib + itself. (No third-party deps by design.)
_STDLIB = set(getattr(__import__("sys"), "stdlib_module_names", set()))
_ALLOWED_ROOTS = _STDLIB | {"installkit"}


def _module_files():
    return sorted(_PKG_DIR.rglob("*.py"))


def test_installkit_has_module_files():
    assert _module_files(), "installkit should contain .py modules"


def test_no_gabagent_import_anywhere():
    """No installkit module may reference the gabagent package (contract is one-directional)."""
    offenders = []
    for f in _module_files():
        src = f.read_text()
        if "gabagent" in src:
            # Allow the word only inside comments/docstrings that talk ABOUT gabagent; forbid it in
            # any actual import statement.
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.Import):
                    if any(a.name.split(".")[0] == "gabagent" for a in node.names):
                        offenders.append(f"{f.name}: import {node.names[0].name}")
                elif isinstance(node, ast.ImportFrom):
                    if (node.module or "").split(".")[0] == "gabagent":
                        offenders.append(f"{f.name}: from {node.module} import …")
    assert not offenders, "installkit must never import gabagent:\n" + "\n".join(offenders)


def test_all_imports_are_stdlib_or_self():
    """Belt-and-braces: every top-level import root must be stdlib or installkit itself."""
    offenders = []
    for f in _module_files():
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            roots = []
            if isinstance(node, ast.Import):
                roots = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative import within installkit — fine
                    continue
                roots = [(node.module or "").split(".")[0]]
            for r in roots:
                if r and r not in _ALLOWED_ROOTS:
                    offenders.append(f"{f.name}: {r}")
    assert not offenders, "installkit may only import stdlib/itself; found:\n" + "\n".join(offenders)
