"""Isolated DuckDuckGo search worker — runs in a SEPARATE process.

The ddgs library's HTTP backend (``primp``) is a Rust/C extension that can hard-crash (segfault) with
no Python traceback, killing whatever process it runs in. Running the search here, out-of-process,
means such a crash exits only this child: the parent (the long-lived voice brain) survives and
degrades to a normal "search failed" instead of dying for the rest of the session.

Protocol: read the query from stdin, print a JSON list of raw hit dicts to stdout, exit 0. Any
failure → exit 2 (and a killing signal → negative returncode); the parent treats any non-zero exit
as "search unavailable" and falls back.
"""
from __future__ import annotations

import json
import sys


def main() -> int:
    query = sys.stdin.read().strip()
    if not query:
        return 2
    try:
        from ddgs import DDGS

        with DDGS() as ddgs:
            hits = list(ddgs.text(query, max_results=10))
    except Exception as e:  # noqa: BLE001 — any in-process error → signal failure to the parent
        sys.stderr.write(f"ddg-worker error: {type(e).__name__}: {e}\n")
        return 2
    json.dump(hits, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
