"""Phase-2 reference plugin — Jellyfin check()/configure() incl. the no-clobber invariant."""
from __future__ import annotations

import httpx

from gabagent.commands.providers.jellyfin_install import INSTALLER
from gabagent.config.models import GabAgentConfig


class _Ask:
    """Scripted stand-in for installkit.wizard: prompts pop from a list, confirms return a fixed answer."""
    def __init__(self, answers=None, confirm=True):
        self._answers = list(answers or [])
        self._confirm = confirm
        self.confirmed = 0

    def prompt(self, text, default=""):
        return self._answers.pop(0) if self._answers else default

    def confirm(self, text, default=True):
        self.confirmed += 1
        return self._confirm


def _resp(status):
    return httpx.Response(status_code=status, request=httpx.Request("GET", "http://x/System/Info/Public"))


def test_check_unconfigured_when_no_api_key(monkeypatch):
    # default base_url is set → probe runs; stub it so no real network is hit.
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp(200))
    r = INSTALLER.check(GabAgentConfig())
    assert r.configured is False
    assert r.reachable is True
    assert any("no API key" in n for n in r.notes)


def test_check_configured_and_reachable(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp(200))
    cfg = GabAgentConfig()
    cfg.jellyfin.api_key = "abc123"
    r = INSTALLER.check(cfg)
    assert r.configured is True and r.reachable is True


def test_check_reachable_false_on_probe_error(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", boom)
    cfg = GabAgentConfig()
    cfg.jellyfin.api_key = "abc123"
    r = INSTALLER.check(cfg)
    assert r.configured is True and r.reachable is False
    assert any("didn't answer" in n for n in r.notes)


def test_check_never_probes_without_base_url(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not probe when base_url is empty")

    monkeypatch.setattr(httpx, "get", boom)
    cfg = GabAgentConfig()
    cfg.jellyfin.base_url = ""
    r = INSTALLER.check(cfg)
    assert r.reachable is None


def test_configure_writes_url_and_key_on_empty():
    cfg = GabAgentConfig()
    cfg.jellyfin.base_url = ""
    ask = _Ask(answers=["http://nas.local:8096", "KEY42"])
    changed = INSTALLER.configure(cfg, ask=ask)
    assert changed is True
    assert cfg.jellyfin.base_url == "http://nas.local:8096"
    assert cfg.jellyfin.api_key == "KEY42"
    assert cfg.jellyfin.enabled is True


def test_configure_no_clobber_declined_leaves_existing():
    cfg = GabAgentConfig()
    cfg.jellyfin.api_key = "ORIGINAL"
    cfg.jellyfin.base_url = "http://orig:8096"
    ask = _Ask(answers=["http://new:8096", "NEWKEY"], confirm=False)  # decline the overwrite
    changed = INSTALLER.configure(cfg, ask=ask)
    assert changed is False
    assert cfg.jellyfin.api_key == "ORIGINAL"          # untouched
    assert cfg.jellyfin.base_url == "http://orig:8096"
    assert ask.confirmed == 1                           # it DID ask before clobbering


def test_configure_overwrite_confirmed():
    cfg = GabAgentConfig()
    cfg.jellyfin.api_key = "ORIGINAL"
    ask = _Ask(answers=["http://new:8096", "NEWKEY"], confirm=True)
    changed = INSTALLER.configure(cfg, ask=ask)
    assert changed is True
    assert cfg.jellyfin.api_key == "NEWKEY"


def test_configure_unchanged_when_same_values():
    cfg = GabAgentConfig()
    cfg.jellyfin.base_url = "http://keep:8096"
    # no existing api_key → no overwrite prompt; re-enter the SAME url, blank key → nothing changes
    ask = _Ask(answers=["http://keep:8096", ""])
    assert INSTALLER.configure(cfg, ask=ask) is False
