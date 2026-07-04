"""Local-file GC for generated images. GA owns cleanup per the image-seam contract (VAC only displays,
never deletes). Best-effort, called on each generation."""
from __future__ import annotations

import time
from pathlib import Path


def gc_old_images(output_dir: str | Path, ttl_secs: int, now: float | None = None) -> int:
    """Delete `*.png` under `output_dir` older than `ttl_secs` (by mtime). Returns the count removed.
    A ttl of 0 (or negative) disables GC. Never raises — a missing dir or an unlinkable file is skipped."""
    if ttl_secs <= 0:
        return 0
    d = Path(output_dir)
    if not d.is_dir():
        return 0
    cutoff = (time.time() if now is None else now) - ttl_secs
    removed = 0
    for p in d.glob("*.png"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            continue
    return removed
