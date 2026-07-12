"""Runtime patch: make mopidy-tidal enumerate ALL of the user's playlists.

mopidy-tidal (through at least 0.3.13.r142) syncs playlists from
``session.user.favorites.playlists_paginated()`` — the **favorited** set only. Playlists the user
*created* but never favorited are therefore invisible to Mopidy's ``core.playlists.as_list`` and to
any client that discovers playlists through it (gabagent's TIDAL voice brain among them). The brain
then truthfully-but-wrongly reports "you have no playlist called X" for a playlist the user really
owns — e.g. a 461-track "Retro Favorites" that plays perfectly by URI but never surfaces by name
(live 2026-07-12). Playback was never broken; only *discovery* was.

TIDAL exposes a purpose-built endpoint that returns the union of created + favorited playlists:
``users/{id}/playlistsAndFavoritePlaylists`` (tidalapi ``LoggedInUser.playlist_and_favorite_playlists``).
It is capped at 50 per call, so we paginate it. This module monkeypatches
``TidalPlaylistsProvider._calculate_added_and_removed_playlist_ids`` to source that complete union
instead of favorites-only, preserving the original add/remove/prune semantics byte-for-byte otherwise.

Fail-soft by design: if tidalapi/mopidy-tidal internals ever change so the patch can't apply, ``apply``
swallows the error and returns False — Mopidy keeps running with its stock (favorites-only) behavior,
never worse than today. Loaded by ``mopidy_launch.py`` before Mopidy's entrypoint; see SETUP_TIDAL.md.
"""
from __future__ import annotations

# NOTE: import nothing from mopidy_tidal / tidalapi at module top level — this file is also imported
# by gabagent's test suite (in a venv WITHOUT those packages) to exercise fetch_all_playlists. The
# heavy imports live inside apply(), which only runs in Mopidy's interpreter.

_MAX_PAGES = 100          # safety valve: 100 * page_size playlists is far beyond any real library
_PAGE_SIZE = 50           # TIDAL's per-call cap for playlistsAndFavoritePlaylists


def fetch_all_playlists(session, page_size: int = _PAGE_SIZE) -> list:
    """The user's COMPLETE playlist set (created ∪ favorited) as tidalapi playlist objects.

    Paginates ``session.user.playlist_and_favorite_playlists(offset, limit)`` — TIDAL caps each call
    at 50, so we walk pages until a short/empty page ends it. Deduped by ``.id`` (a playlist can only
    appear once, but a boundary shift between calls must never double-count). A hard page cap prevents
    an unbounded loop if the endpoint ever returns full pages forever. Pure except for the injected
    ``session``, so it is unit-tested with a fake session (no tidalapi needed)."""
    out: dict = {}
    offset = 0
    for _ in range(_MAX_PAGES):
        batch = session.user.playlist_and_favorite_playlists(offset=offset, limit=page_size)
        if not batch:
            break
        for pl in batch:
            out[pl.id] = pl
        if len(batch) < page_size:
            break
        offset += page_size
    return list(out.values())


def _make_patched_calc():
    """Build the replacement for TidalPlaylistsProvider._calculate_added_and_removed_playlist_ids.

    Identical to the stock method except the source of ``updated_playlists`` is the full created ∪
    favorited union (via fetch_all_playlists) instead of favorites-only. The add/remove/prune bookkeeping
    below is preserved exactly so sync-delta behavior is unchanged."""
    def _calculate_added_and_removed_playlist_ids(self):
        session = self.backend.session
        try:
            updated_playlists = fetch_all_playlists(session)
        except Exception as e:  # noqa: BLE001
            # Runtime fail-soft: if the union endpoint is unavailable (older tidalapi, API change) the
            # patched sync must not BREAK playlist listing — fall back to stock favorites-only, the exact
            # source the unpatched method used, so behavior degrades to today's, never worse.
            import sys
            sys.stderr.write(f"[mopidy_tidal_all_playlists] union fetch failed ({e!r}); "
                             f"falling back to favorites-only for this sync\n")
            updated_playlists = session.user.favorites.playlists_paginated()

        self._current_tidal_playlists = updated_playlists
        updated_ids = set(pl.id for pl in updated_playlists)

        if not self._playlists_metadata:
            return updated_ids, set()

        current_ids = set(uri.split(":")[-1] for uri in self._playlists_metadata.keys())
        added_ids = updated_ids.difference(current_ids)
        removed_ids = current_ids.difference(updated_ids)

        self._playlists_metadata.prune(
            *[
                uri
                for uri in self._playlists_metadata.keys()
                if uri.split(":")[-1] in removed_ids
            ]
        )

        return added_ids, removed_ids

    return _calculate_added_and_removed_playlist_ids


def apply() -> bool:
    """Monkeypatch mopidy-tidal to enumerate created + favorited playlists. Returns True if applied.

    Fail-soft: any error (package moved, method renamed, tidalapi absent) is swallowed and False is
    returned so Mopidy still starts with its stock favorites-only behavior — never worse than today."""
    try:
        from mopidy_tidal import playlists as _pl
        _pl.TidalPlaylistsProvider._calculate_added_and_removed_playlist_ids = _make_patched_calc()
        return True
    except Exception as e:  # noqa: BLE001 — startup must never crash on a failed patch
        import sys
        sys.stderr.write(f"[mopidy_tidal_all_playlists] patch NOT applied ({e!r}); "
                         f"mopidy-tidal stays favorites-only\n")
        return False
