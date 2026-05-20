from __future__ import annotations
import json
import time
from pathlib import Path

_BRIDGE_DIR = Path.home() / ".config" / "gabagent" / "bridge"
_INBOX = _BRIDGE_DIR / "from_claude.jsonl"   # Claude Code → Gab
_OUTBOX = _BRIDGE_DIR / "from_gab.jsonl"     # Gab → Claude Code


def _ensure() -> None:
    _BRIDGE_DIR.mkdir(parents=True, exist_ok=True)


def claude_to_gab(message: str, topic: str = "note") -> None:
    """Write a message from Claude Code into Gab's inbox."""
    _ensure()
    entry = {"ts": time.time(), "from": "claude", "topic": topic, "message": message}
    with _INBOX.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def gab_to_claude(message: str, topic: str = "note") -> None:
    """Write a message from Gab into Claude Code's outbox."""
    _ensure()
    entry = {"ts": time.time(), "from": "gab", "topic": topic, "message": message}
    with _OUTBOX.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def read_inbox(mark_read: bool = True) -> list[dict]:
    """Read all unread messages in Gab's inbox (from Claude Code)."""
    _ensure()
    if not _INBOX.exists():
        return []
    lines = _INBOX.read_text().splitlines()
    messages = [json.loads(l) for l in lines if l.strip()]
    if mark_read and messages:
        _INBOX.unlink()
    return messages


def read_outbox(mark_read: bool = True) -> list[dict]:
    """Read all messages Gab has sent (for Claude Code to consume)."""
    _ensure()
    if not _OUTBOX.exists():
        return []
    lines = _OUTBOX.read_text().splitlines()
    messages = [json.loads(l) for l in lines if l.strip()]
    if mark_read and messages:
        _OUTBOX.unlink()
    return messages


def clear_outbox() -> None:
    if _OUTBOX.exists():
        _OUTBOX.unlink()
