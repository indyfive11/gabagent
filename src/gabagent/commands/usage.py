"""A tiny per-host usage tally for catalog commands.

Powers the "hot set" promotion: commands the user actually runs bubble up into the in-context
index, so frequently-used skills surface inline without curation — and the index stays flat
regardless of how many skills are installed. Best-effort: any I/O failure is swallowed.
"""
from __future__ import annotations
import json
from gabagent.config.paths import data_dir

_PATH = data_dir() / "command_usage.json"


def _load() -> dict[str, int]:
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def record(command_id: str) -> None:
    if not command_id:
        return
    try:
        counts = _load()
        counts[command_id] = counts.get(command_id, 0) + 1
        _PATH.write_text(json.dumps(counts), encoding="utf-8")
    except Exception:
        pass


def top(n: int) -> list[str]:
    """The n most-used command ids, most-used first."""
    counts = _load()
    return [cid for cid, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]
