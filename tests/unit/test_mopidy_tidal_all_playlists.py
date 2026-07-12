"""Unit tests for the mopidy-tidal all-playlists patch's pagination logic.

The patch module runs inside Mopidy's interpreter (which has mopidy_tidal + tidalapi); this suite
runs in gabagent's venv, which has neither. So we load the module by path and exercise ONLY
``fetch_all_playlists`` — it's pure over an injected session, so a fake session covers the risky
logic (page-boundary termination, dedup, the anti-infinite-loop cap) with no external deps.
"""
import importlib.util
import os

import pytest

_MOD_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "contrib", "mopidy", "mopidy_tidal_all_playlists.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("mopidy_tidal_all_playlists", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load()


class _PL:
    def __init__(self, pid, name=""):
        self.id = pid
        self.name = name


class _Favs:
    """Fake tidalapi user backing playlist_and_favorite_playlists with a fixed page size."""

    def __init__(self, playlists, page_size=50):
        self._pl = playlists
        self._page = page_size
        self.calls = []

    def playlist_and_favorite_playlists(self, offset=0, limit=50):
        self.calls.append((offset, limit))
        return self._pl[offset:offset + min(limit, self._page)]


class _Session:
    def __init__(self, user):
        self.user = user


def _session(n, page_size=50):
    return _Session(_Favs([_PL(f"id{i}", f"pl{i}") for i in range(n)], page_size=page_size))


def test_single_short_page():
    s = _session(26)
    out = mod.fetch_all_playlists(s, page_size=50)
    assert len(out) == 26
    assert s.user.calls == [(0, 50)]  # one call, short page ends it


def test_exactly_one_full_page_then_empty():
    # 50 == page_size forces a second call, which returns [] and terminates (no infinite loop).
    s = _session(50)
    out = mod.fetch_all_playlists(s, page_size=50)
    assert len(out) == 50
    assert s.user.calls == [(0, 50), (50, 50)]


def test_multi_page_union():
    s = _session(76)  # the real-world case: 50 + 26
    out = mod.fetch_all_playlists(s, page_size=50)
    assert len(out) == 76
    assert {p.id for p in out} == {f"id{i}" for i in range(76)}
    assert s.user.calls == [(0, 50), (50, 50)]


def test_dedup_by_id():
    dupe = [_PL("a"), _PL("b"), _PL("a")]  # same id twice must collapse to one
    s = _Session(_Favs(dupe, page_size=50))
    out = mod.fetch_all_playlists(s, page_size=50)
    assert len(out) == 2
    assert {p.id for p in out} == {"a", "b"}


def test_empty_library():
    s = _session(0)
    out = mod.fetch_all_playlists(s, page_size=50)
    assert out == []
    assert s.user.calls == [(0, 50)]


def test_page_cap_bounds_runaway():
    # A pathological endpoint that always returns a full page must still terminate at the cap.
    class _Endless:
        def __init__(self):
            self.n = 0

        def playlist_and_favorite_playlists(self, offset=0, limit=50):
            self.n += 1
            return [_PL(f"e{offset}-{i}") for i in range(limit)]  # always full → never self-terminates

    e = _Endless()
    out = mod.fetch_all_playlists(_Session(e), page_size=50)
    assert e.n == mod._MAX_PAGES            # stopped by the safety valve, not an infinite loop
    assert len(out) == mod._MAX_PAGES * 50


def test_apply_is_failsoft_without_mopidy_tidal():
    # In this venv mopidy_tidal isn't importable, so apply() must swallow and return False, not raise.
    assert mod.apply() is False


class _FavOnly:
    """Fake user whose union endpoint is BROKEN but favorites still works — the older-tidalapi case."""

    def __init__(self, favs):
        self._favs = favs
        self.favorites = self  # so user.favorites.playlists_paginated() resolves to us

    def playlist_and_favorite_playlists(self, offset=0, limit=50):
        raise AttributeError("playlist_and_favorite_playlists not supported")

    def playlists_paginated(self):
        return list(self._favs)


class _FakeMeta(dict):
    def prune(self, *uris):
        for u in uris:
            self.pop(u, None)


def test_patched_calc_runtime_failsoft_to_favorites():
    # The patched sync method must not break listing when the union endpoint is unavailable — it falls
    # back to the exact stock favorites-only source, never worse than the unpatched behavior.
    favs = [_PL("f1", "fav1"), _PL("f2", "fav2")]

    class _Self:
        pass

    s = _Self()
    s.backend = type("B", (), {"session": _Session(_FavOnly(favs))})()
    s._playlists_metadata = _FakeMeta()  # empty → returns (updated_ids, set())

    patched = mod._make_patched_calc()
    added, removed = patched(s)
    assert added == {"f1", "f2"}          # favorites came through despite the broken union endpoint
    assert removed == set()
    assert {p.id for p in s._current_tidal_playlists} == {"f1", "f2"}
