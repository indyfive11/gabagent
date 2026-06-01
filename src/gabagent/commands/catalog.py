"""The live set of available commands, keyed by id."""
from __future__ import annotations
from gabagent.commands.model import Command


class CommandCatalog:
    def __init__(self) -> None:
        self._cmds: dict[str, Command] = {}

    def add(self, cmd: Command) -> None:
        self._cmds[cmd.id] = cmd

    def get(self, command_id: str) -> Command | None:
        return self._cmds.get(command_id)

    def all(self) -> list[Command]:
        return list(self._cmds.values())

    def by_domain(self, domain: str) -> list[Command]:
        return [c for c in self._cmds.values() if c.domain == domain]

    def ids(self) -> list[str]:
        return list(self._cmds.keys())

    def summaries(self, domain: str | None = None) -> list[dict]:
        """Compact catalog for the model (id, domain, summary, tier, param hints)."""
        cmds = self.by_domain(domain) if domain else self.all()
        out = []
        for c in sorted(cmds, key=lambda c: c.id):
            out.append({
                "id": c.id,
                "domain": c.domain,
                "summary": c.summary,
                "tier": c.tier,
                "params": [
                    {
                        "name": s.name,
                        "type": s.type,
                        "required": s.required,
                        **({"enum": list(s.enum)} if s.enum else {}),
                    }
                    for s in c.params
                ],
            })
        return out
