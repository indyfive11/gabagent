"""Structured fact store for the tiered-memory layer.

A Fact is one durable memory line plus the metadata the reconciler (and, later, the Tier-0 weighting
in P3) needs: how often and across which weeks/rooms it has recurred, and whether the user pinned it.
The store is append-mostly JSONL, rewritten wholesale on each reconcile (like the persona INDEX), so a
mid-write reader never sees a half-merged set. P2 maintains the metadata but does NOT weight or escalate
— that is P3.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path


def normalize(text: str) -> str:
    """Identity key for dedup/merge: lowercased, de-bulleted, whitespace-collapsed."""
    t = (text or "").strip().lstrip("-*•").strip().lower()
    return " ".join(t.split())


def iso_week(ts: float) -> str:
    """The 'YYYY-Www' bucket a timestamp falls in — the unit the habit signal counts DISTINCT weeks over."""
    import datetime
    iso = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isocalendar()
    return f"{iso[0]:04d}-W{iso[1]:02d}"


@dataclass
class Fact:
    text: str
    sources: list[str] = field(default_factory=list)  # room_ids that contributed this fact
    first_seen: float = 0.0
    last_seen: float = 0.0
    hits: int = 1                                      # times re-observed across reconciles
    seen_weeks: list[str] = field(default_factory=list)  # distinct ISO weeks observed (habit signal)
    pinned: bool = False                              # user-explicit ⇒ never decays/prunes

    @property
    def key(self) -> str:
        return normalize(self.text)

    def observe(self, ts: float, room_id: str | None) -> None:
        """Record another sighting of this fact: bump hits, extend last_seen, week, and source set."""
        self.last_seen = ts
        self.hits += 1
        wk = iso_week(ts)
        if wk not in self.seen_weeks:
            self.seen_weeks.append(wk)
        rid = room_id or "default"
        if rid not in self.sources:
            self.sources.append(rid)


class FactStore:
    """Read/write the JSONL fact file for one tier directory. Fully defensive — a corrupt or missing
    file reads as empty, never raises."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> list[Fact]:
        out: list[Fact] = []
        try:
            if not self._path.exists():
                return out
            for line in self._path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    out.append(Fact(**{k: d.get(k) for k in Fact.__dataclass_fields__ if k in d}))
                except Exception:
                    continue  # skip a single bad line, keep the rest
        except Exception:
            return []
        return out

    def save(self, facts: list[Fact]) -> None:
        """Atomic rewrite: write a temp sibling then os.replace, so a reader never sees a partial file."""
        import os
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f".{self._path.name}.tmp")
            body = "\n".join(json.dumps(asdict(f), ensure_ascii=False) for f in facts)
            tmp.write_text(body + ("\n" if body else ""), encoding="utf-8")
            os.replace(tmp, self._path)
        except Exception:
            pass
