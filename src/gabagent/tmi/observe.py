"""TMI receipts — a durable, append-only audit trail for the reconcile + escalate passes.

These passes run in the DETACHED shutdown child, which has no live voice session, so their work never
reaches voice_debug.jsonl. Without a receipt the only evidence an escalation ran is the on-disk store
mutating — and a *rejected* escalation (the judge declined every nominee) leaves no trace at all, so you
can't tell "ran and correctly kept quiet" from "never ran / threw". tmi_log drops one JSONL line per
pass to tmi/events.jsonl so a shakedown can read exactly what was nominated, what the judge kept, and
what landed in Tier 0.

Best-effort and defensive by contract — like the passes it observes: never raises, never blocks
shutdown. Bounded: the trail is trimmed back to the last _MAX_LINES once it grows past 2x that.
"""
from __future__ import annotations

import json
from datetime import datetime

_MAX_LINES = 2000  # ~a month of shutdowns; trail is trimmed to this when it grows past 2x


def tmi_log(event: str, **fields) -> None:
    """Append one receipt line (ISO-`T` ts + event + fields) to tmi/events.jsonl. Never raises."""
    try:
        from gabagent.config.paths import tmi_dir
        path = tmi_dir() / "events.jsonl"
        rec = {"ts": datetime.now().isoformat(timespec="milliseconds"), "event": event}
        rec.update(fields)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _trim(path)
    except Exception:
        pass


def _trim(path) -> None:
    """Keep the trail bounded: once it exceeds 2x the cap, rewrite atomically with the last _MAX_LINES."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) > _MAX_LINES * 2:
            import os
            tmp = path.with_name(f".{path.name}.tmp")
            tmp.write_text("\n".join(lines[-_MAX_LINES:]) + "\n", encoding="utf-8")
            os.replace(tmp, path)
    except Exception:
        pass
