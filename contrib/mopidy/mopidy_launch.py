#!/usr/bin/python
"""Mopidy launcher that applies the mopidy-tidal all-playlists patch, then runs Mopidy normally.

Drop-in for ``/usr/bin/mopidy``: replicates that console script's entrypoint exactly (same argv
scrub, same ``mopidy.__main__:main``, same default config search — no args added), but first loads
``mopidy_tidal_all_playlists`` so created-but-not-favorited playlists become discoverable (see that
module's docstring and SETUP_TIDAL.md).

Wire it from the user unit:

    ExecStart=/usr/bin/python /home/rob/dev/gabagent/contrib/mopidy/mopidy_launch.py

Must run under the SAME interpreter as Mopidy (system ``/usr/bin/python`` for the AUR package) so
mopidy_tidal / tidalapi are importable. Fail-soft: if the patch can't load, Mopidy still starts stock.
"""
import os
import re
import sys

# Make this directory importable so the sibling patch module is found regardless of CWD.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    import mopidy_tidal_all_playlists
    mopidy_tidal_all_playlists.apply()
except Exception as e:  # noqa: BLE001 — never block Mopidy startup on the patch
    sys.stderr.write(f"[mopidy_launch] all-playlists patch skipped: {e!r}\n")

from mopidy.__main__ import main

if __name__ == "__main__":
    sys.argv[0] = re.sub(r"(-script\.pyw|\.exe)?$", "", sys.argv[0])
    sys.exit(main())
