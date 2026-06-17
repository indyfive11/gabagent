"""web_search crash-isolation: the ddgs/primp search runs in a child process so a native crash
degrades to a per-turn failure instead of killing the (voice-brain) process (2026-06-17 crash)."""
import asyncio
import json
import sys
import types

import pytest

from gabagent.tools import search_tools
from gabagent.tools.search_tools import WebSearchTool, _ddg_search_subprocess


class _FakeProc:
    def __init__(self, out=b"", rc=0, hang=False):
        self._out, self.returncode, self._hang, self.killed = out, rc, hang, False

    async def communicate(self, data=None):
        if self._hang:
            await asyncio.sleep(100)
        return self._out, b""

    def kill(self):
        self.killed = True


def _patch_proc(monkeypatch, proc):
    async def _fake_exec(*a, **k):
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)


# -- the isolation helper --------------------------------------------------

async def test_subprocess_parses_json_list(monkeypatch):
    hits = [{"title": "T", "href": "u", "body": "b"}]
    _patch_proc(monkeypatch, _FakeProc(out=json.dumps(hits).encode(), rc=0))
    assert await _ddg_search_subprocess("q") == hits


async def test_subprocess_none_when_child_crashes(monkeypatch):
    # Negative returncode = killed by signal (the segfault case). Parent must survive → None.
    _patch_proc(monkeypatch, _FakeProc(out=b"", rc=-11))
    assert await _ddg_search_subprocess("q") is None


async def test_subprocess_none_on_nonzero_exit(monkeypatch):
    _patch_proc(monkeypatch, _FakeProc(out=b"", rc=2))
    assert await _ddg_search_subprocess("q") is None


async def test_subprocess_none_and_kills_on_timeout(monkeypatch):
    proc = _FakeProc(hang=True)
    _patch_proc(monkeypatch, proc)
    assert await _ddg_search_subprocess("q", timeout=0.05) is None
    assert proc.killed is True


async def test_subprocess_none_on_garbage_stdout(monkeypatch):
    _patch_proc(monkeypatch, _FakeProc(out=b"not json", rc=0))
    assert await _ddg_search_subprocess("q") is None


# -- the worker contract (real child process, no network) ------------------

def test_worker_empty_query_exits_2():
    import subprocess
    p = subprocess.run([sys.executable, "-m", "gabagent.tools._ddg_search_worker"],
                       input=b"", capture_output=True)
    assert p.returncode == 2


# -- web_search degrades, never raises -------------------------------------

def _ctx(tmp_path, searxng=""):
    return types.SimpleNamespace(cwd=tmp_path, config=types.SimpleNamespace(searxng_url=searxng))


def _isolate_cache(tmp_path, monkeypatch):
    # web_cache_dir() uses config_dir(), not XDG_DATA_HOME — redirect it so tests don't read/write the
    # real cache (and don't collide across tests via a stale cached query).
    import gabagent.config.paths as paths
    cache = tmp_path / "web_cache"; cache.mkdir()
    monkeypatch.setattr(paths, "web_cache_dir", lambda: cache)


async def test_web_search_formats_hits(tmp_path, monkeypatch):
    _isolate_cache(tmp_path, monkeypatch)
    monkeypatch.setattr(search_tools, "_ddg_search_subprocess",
                        lambda q, **k: _aval([{"title": "Camp", "href": "ex.com", "body": "nice spot"}]))
    res = await WebSearchTool().execute(_ctx(tmp_path), query="camping spots")
    assert "[Camp](ex.com)" in res.output and "nice spot" in res.output and res.error is None


async def test_web_search_degrades_when_child_dies(tmp_path, monkeypatch):
    # Child crashed (helper → None) and no SearxNG configured → a clean error, NOT a raise/crash.
    _isolate_cache(tmp_path, monkeypatch)
    monkeypatch.setattr(search_tools, "_ddg_search_subprocess", lambda q, **k: _aval(None))
    res = await WebSearchTool().execute(_ctx(tmp_path, searxng=""), query="no fallback configured")
    assert res.error and "unavailable" in res.error.lower()


def _aval(v):
    async def _coro():
        return v
    return _coro()
