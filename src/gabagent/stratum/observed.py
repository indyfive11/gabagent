"""Observed-Habits store (Stratum Tier-0.5) — a subordinate, machine-wide store of workflow-habit
observations about the user. NEVER authoritative: it is surfaced/injected as an explicitly
subordinate block and never overrides a durable rule or an explicit instruction.

Format (Stratum §7.3): one ``## <observation>`` block per habit; the heading text IS the observation
and is immutable after accretion. Reinforcement touches only ``hits``/``last_seen``/``seen_weeks``.

Scoring reuses only the general :func:`gabagent.tmi.weights.decay`; the rest is purpose-built for
coding habits (a conflict penalty, no cross-room/volatility terms).
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from gabagent.tmi.weights import decay

_HEADING_MAX = 200


def _today() -> str:
    return date.today().isoformat()


def iso_week(d: str | None = None) -> str:
    dt = datetime.strptime(d, "%Y-%m-%d").date() if d else date.today()
    y, w, _ = dt.isocalendar()
    return f"{y}-W{w:02d}"


def _epoch(iso_day: str) -> float:
    try:
        return datetime.strptime(iso_day, "%Y-%m-%d").timestamp()
    except Exception:
        return time.time()


def normalize(text: str) -> str:
    """Match key: lowercased, de-bulleted, whitespace-collapsed heading text."""
    t = text.strip().lstrip("#").strip().lstrip("-*").strip().lower()
    return re.sub(r"\s+", " ", t)


@dataclass
class Habit:
    heading: str
    hits: int = 1
    seen_weeks: list[str] = field(default_factory=list)
    conflict_count: int = 0
    state: str = "accreting"          # accreting | eligible | nominated | rejected | candidate
    origin: str = "observed"          # observed | definitive
    first_seen: str = field(default_factory=_today)
    last_seen: str = field(default_factory=_today)
    accreted: str = field(default_factory=_today)

    def score(self, now: float, halflife_days: float) -> float:
        return (
            decay(_epoch(self.last_seen), now, halflife_days)
            + math.log1p(max(0, self.hits))
            + (1.0 if self.origin == "definitive" else 0.0)
            - self.conflict_count
        )


class ObservedStore:
    """Markdown-backed observed-habits store. Lazy: the file is created only on the first save."""

    def __init__(self, path: Path):
        self._path = path

    # ---- persistence ---------------------------------------------------
    def load(self) -> list[Habit]:
        if not self._path.exists():
            return []
        try:
            text = self._path.read_text(encoding="utf-8")
        except Exception:
            return []
        return _parse(text)

    def save(self, habits: list[Habit]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(_render(habits), encoding="utf-8")

    # ---- accretion -----------------------------------------------------
    def accrete(self, observation: str, *, origin: str = "observed", state: str = "accreting") -> None:
        """Add a new habit or reinforce a matching one. Reinforcement touches ONLY hits/last_seen/
        seen_weeks — never the heading (immutable) or other fields (Stratum §7.3 hard rule)."""
        heading = observation.strip().lstrip("#").strip()[:_HEADING_MAX].strip()
        if not heading:
            return
        habits = self.load()
        key = normalize(heading)
        wk, today = iso_week(), _today()
        for h in habits:
            if normalize(h.heading) == key:
                h.hits += 1
                h.last_seen = today
                if wk not in h.seen_weeks:
                    h.seen_weeks.append(wk)
                self.save(habits)
                return
        habits.append(Habit(
            heading=heading, hits=1, seen_weeks=[wk], state=state, origin=origin,
        ))
        self.save(habits)

    def mark_conflict(self, observation: str) -> bool:
        habits = self.load()
        key = normalize(observation)
        for h in habits:
            if normalize(h.heading) == key:
                h.conflict_count += 1
                self.save(habits)
                return True
        return False

    # ---- maintenance ---------------------------------------------------
    def prune(self, soft_cap: int, hard_cap: int, halflife_days: float) -> None:
        """Prune lowest-scoring NON-protected entries when over the soft cap. Protected = state
        eligible/nominated (a candidate awaiting the user's decision cannot be pruned)."""
        habits = self.load()
        cap = min(soft_cap, hard_cap)
        if len(habits) <= cap:
            return
        now = time.time()
        protected = [h for h in habits if h.state in ("eligible", "nominated")]
        prunable = sorted(
            (h for h in habits if h.state not in ("eligible", "nominated")),
            key=lambda h: h.score(now, halflife_days),
            reverse=True,
        )
        keep = protected + prunable[: max(0, cap - len(protected))]
        # preserve original order for stability
        order = {id(h): i for i, h in enumerate(habits)}
        keep.sort(key=lambda h: order[id(h)])
        self.save(keep)

    def advance(self, adv_days: int, adv_hits: int, adv_weeks: int) -> list[Habit]:
        """Set state=eligible on every entry that passes the gate (§7.6). Returns the newly-eligible."""
        habits = self.load()
        now = date.today()
        newly: list[Habit] = []
        for h in habits:
            if h.state in ("nominated", "rejected", "eligible", "candidate"):
                continue
            age = (now - datetime.strptime(h.accreted, "%Y-%m-%d").date()).days
            if (age >= adv_days and h.hits >= adv_hits
                    and len(set(h.seen_weeks)) >= adv_weeks and h.conflict_count == 0):
                h.state = "eligible"
                newly.append(h)
        if newly:
            self.save(habits)
        return newly

    # ---- rendering -----------------------------------------------------
    def render_block(self, *, top: int = 35, budget: int = 3000, halflife_days: float = 30) -> str:
        """The SUBORDINATE injection block: top-ranked non-candidate/non-rejected habits, capped by
        count and byte budget. Empty string when there is nothing to surface."""
        now = time.time()
        live = [h for h in self.load() if h.state not in ("candidate", "rejected")]
        if not live:
            return ""
        live.sort(key=lambda h: h.score(now, halflife_days), reverse=True)
        lines = [
            "OBSERVED HABITS (Tier 0.5 — SUBORDINATE; observations about how the user works, NOT rules.",
            "NEVER overrides an explicit instruction, a durable rule, or a critical task. Decays when wrong):",
        ]
        out, size = [], len("\n".join(lines))
        for h in live[:top]:
            line = f"- {h.heading}"
            if size + len(line) + 1 > budget:
                break
            out.append(line)
            size += len(line) + 1
        if not out:
            return ""
        return "\n".join(lines + out)


# ---- markdown (de)serialization ----------------------------------------
_FIELDS_INT = {"hits", "conflict_count"}


def _render(habits: list[Habit]) -> str:
    parts = ["<!-- Stratum observed-habits store. Headings are immutable; edit fields with care. -->", ""]
    for h in habits:
        parts.append(f"## {h.heading}")
        parts.append(f"- first_seen: {h.first_seen}")
        parts.append(f"- last_seen: {h.last_seen}")
        parts.append(f"- accreted: {h.accreted}")
        parts.append(f"- hits: {h.hits}")
        parts.append(f"- seen_weeks: {', '.join(h.seen_weeks)}")
        parts.append(f"- conflict_count: {h.conflict_count}")
        parts.append(f"- origin: {h.origin}")
        parts.append(f"- state: {h.state}")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _parse(text: str) -> list[Habit]:
    habits: list[Habit] = []
    cur: Habit | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            if cur is not None:
                habits.append(cur)
            cur = Habit(heading=line[3:].strip(), hits=0, seen_weeks=[])
            continue
        if cur is None:
            continue
        m = re.match(r"^-\s*([a-z_]+):\s*(.*)$", line)
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if k in _FIELDS_INT:
            try:
                setattr(cur, k, int(v))
            except ValueError:
                pass
        elif k == "seen_weeks":
            cur.seen_weeks = [w.strip() for w in v.split(",") if w.strip()]
        elif k in ("state", "origin", "first_seen", "last_seen", "accreted"):
            if v:
                setattr(cur, k, v)
    if cur is not None:
        habits.append(cur)
    return habits
