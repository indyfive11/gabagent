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

    def domains(self) -> list[str]:
        """Sorted unique domains present in the catalog (the index lives at this granularity)."""
        return sorted({c.domain for c in self._cmds.values()})

    def search(self, query: str, limit: int = 12) -> list[Command]:
        """Keyword lookup over id / summary / examples — the on-demand index lookup. Ranks by how
        many query terms match, with whole-word/id-prefix matches weighted higher."""
        terms = [t for t in query.lower().split() if t]
        if not terms:
            return []
        scored: list[tuple[int, Command]] = []
        for c in self._cmds.values():
            hay = " ".join([c.id.replace(".", " ").replace("_", " "), c.summary, *c.examples]).lower()
            score = 0
            for t in terms:
                if t in hay:
                    score += 1
                if c.id.lower().startswith(t) or f" {t}" in f" {c.id.lower()}":
                    score += 2
            if score:
                scored.append((score, c))
        scored.sort(key=lambda sc: (-sc[0], sc[1].id))
        return [c for _, c in scored[:limit]]

    def index(self) -> list[dict]:
        """Compact per-domain index — {domain, count, commands(ids only)}. What a blank
        list_capabilities returns; params/details come from summaries(domain) or search()."""
        out = []
        for dom in self.domains():
            cmds = sorted(self.by_domain(dom), key=lambda c: c.id)
            out.append({"domain": dom, "count": len(cmds), "commands": [c.id for c in cmds]})
        return out

    def featured(self) -> list[Command]:
        return [c for c in sorted(self._cmds.values(), key=lambda c: c.id) if c.featured]

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
