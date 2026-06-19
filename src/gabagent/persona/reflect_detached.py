"""Detached persona reflection — runs in a short-lived child that OUTLIVES the brain.

The brain's shutdown window is hostile to learning: the voice front-end SIGTERMs the brain and
SIGKILLs it ~5s later (voice-agent `http_brain_client.aclose`), while a top-rung (opus/max)
reflection call needs far longer. Running reflection in the dying brain's `finally` loses that race
every time and never persists anything.

So instead the brain serializes the recent transcript to a small handoff file and spawns this module
(`python -m gabagent.persona.reflect_detached <handoff.json>`) with `start_new_session=True`, then
exits. This process lives in its OWN session/process group, so killing the brain — by pid or by its
group — never touches it. It finishes the reflection on its own time and writes
INDEX.md / journal.md / voice_hint.json.

Self-contained: it loads config independently (settings.json), rebuilds a minimal context mirroring the
live voice ctx, and reuses `PersonaManager.reflect_from_ctx`. Best-effort — any failure just exits
non-zero; the brain is already gone and nothing waits on this process.

Known limitation: `start_new_session` escapes the process group but NOT the systemd cgroup, so a full
`systemctl --user stop voice-agent` (which kills the whole service cgroup) can still reap this child.
It survives the common case — VAC restarting/replacing just the brain while the service stays up.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path


def _load_turns(handoff: Path) -> list:
    """Rebuild the handed-off transcript as ChatMessages (role/content only — all reflection needs)."""
    data = json.loads(handoff.read_text(encoding="utf-8"))
    from gabagent.api.models import ChatMessage

    return [ChatMessage(role=t["role"], content=t["content"]) for t in data.get("turns", [])]


def _build_ctx(turns: list):
    """A minimal context exposing exactly the attributes the reflection path reads: config, a fresh
    client, a session that yields the handed-off turns, and the ladder-routing fields consulted by
    `_reflection_client` → `ModelRouter.assemble` → `_active_client`. local_client is None and
    degraded is empty — reflection wants the TOP (cloud) rung, not the local floor, so this resolves
    to the same opus/max rung the live brain would pick."""
    from gabagent.config.loader import load_config
    from gabagent.api.factory import build_client
    from gabagent.api.rate_limit import UsageTracker

    cfg = load_config({})
    simple_model = cfg.claude.ladder[0].model if cfg.provider == "claude" else cfg.router.simple_model
    rl = UsageTracker(simple_model=simple_model)
    client = build_client(cfg, rl)
    return types.SimpleNamespace(
        config=cfg, client=client, rate_limiter=rl,
        session=types.SimpleNamespace(messages=lambda: list(turns)),
        local_floor=getattr(cfg, "local_floor", False),
        local_client=None, local_mode=False, clients={},
        degraded_backends=set(),
    )


async def _run(handoff: Path) -> None:
    turns = _load_turns(handoff)
    if not turns:
        return
    from gabagent.persona.manager import PersonaManager

    await PersonaManager().reflect_from_ctx(_build_ctx(turns))


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        return 2
    handoff = Path(argv[0])
    try:
        asyncio.run(_run(handoff))
        return 0
    except Exception:
        return 1
    finally:
        try:
            handoff.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
