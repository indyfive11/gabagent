"""Build the live CommandCatalog: probe first-party providers + load attested skill plugins.

Provider detection runs concurrently with short timeouts; one provider failing never aborts
discovery. Skill-plugin loading (attested, data-only) is wired in Phase 1.
"""
from __future__ import annotations
import asyncio
from typing import TYPE_CHECKING

from gabagent.commands.catalog import CommandCatalog

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


async def discover_capabilities(ctx: AgentContext, providers=None) -> CommandCatalog:
    if providers is None:
        from gabagent.commands.providers import PROVIDERS
        providers = PROVIDERS

    catalog = CommandCatalog()

    async def _probe(prov):
        try:
            if await prov.detect(ctx):
                return list(prov.commands(ctx))
        except Exception:
            return []
        return []

    if providers:
        results = await asyncio.gather(*[_probe(p) for p in providers], return_exceptions=True)
        for r in results:
            if isinstance(r, list):
                for cmd in r:
                    catalog.add(cmd)

    # Phase 1: load enabled+attested skill plugins here.
    try:
        from gabagent.commands.skills.loader import load_enabled_skills
        for cmd in load_enabled_skills(ctx):
            catalog.add(cmd)
    except Exception:
        pass  # skills subsystem not present yet (Phase 0) or load failure — never block discovery

    return catalog
