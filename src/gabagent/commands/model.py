"""Declarative command/capability model for the voice command framework.

A `Command` is one invokable capability (e.g. "jellyfin.search", "media.playpause").
It carries its safety `tier` (the authoritative value the gate reads), a `backend`
describing HOW it runs (a tagged union), parameter `Slot`s, and a `detect` rule for
availability. Backends are split into DATA-only kinds (shell/http/launch — expressible
in third-party TOML skill plugins and statically attestable) and FIRST-PARTY-only kinds
(python/browser — real code, never accepted from plugins).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

# Backend kinds a declarative (third-party) skill plugin may use.
DATA_BACKENDS = ("shell", "http", "launch")
# Backend kinds only first-party providers may use (they reference real Python).
CODE_BACKENDS = ("python", "browser")


@dataclass(frozen=True)
class ShellBackend:
    argv: list[str]
    timeout: float = 15.0
    kind: str = "shell"


@dataclass(frozen=True)
class HttpBackend:
    method: str = "GET"
    path: str = ""                         # full URL, or relative path joined to an auth profile's base
    query: dict[str, str] = field(default_factory=dict)
    json_body: dict[str, Any] | None = None
    auth: str = ""                         # auth profile id (e.g. "jellyfin"); "" => no auth, path is a full URL
    kind: str = "http"


@dataclass(frozen=True)
class LaunchBackend:
    target: str = ""                       # a URI or .desktop id passed to xdg-open
    kind: str = "launch"


@dataclass(frozen=True)
class PyBackend:                           # first-party only
    ref: str = ""                          # dotted "module:callable" taking (ctx, **slots)
    kind: str = "python"


@dataclass(frozen=True)
class BrowserBackend:                       # first-party only
    ref: str = ""                          # dotted "module:callable" taking (ctx, **slots)
    kind: str = "browser"


Backend = ShellBackend | HttpBackend | LaunchBackend | PyBackend | BrowserBackend


@dataclass(frozen=True)
class Slot:
    name: str
    type: str = "string"                   # string | integer | number | boolean | enum
    required: bool = False
    enum: tuple[str, ...] | None = None
    description: str = ""
    default: Any = None


@dataclass(frozen=True)
class Detect:
    kind: str = "always"                   # binary | http | file | always
    value: str = ""                        # binary name / probe URL / file path
    timeout: float = 2.0


@dataclass(frozen=True)
class Command:
    id: str
    domain: str
    summary: str
    tier: int                              # 1 | 2 | 3 — authoritative for the gate
    backend: Backend
    detect: Detect = field(default_factory=Detect)
    params: list[Slot] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    structured: bool = False               # True => result is JSON the model parses
    requires_confirm_surface: bool = False
