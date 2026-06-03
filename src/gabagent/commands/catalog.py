"""The live set of available commands, keyed by id."""
from __future__ import annotations
from gabagent.commands.model import Command

# Plausible-but-wrong ids the model reliably guesses → the real command. Saves a wasted
# unknown-command turn + list_capabilities recovery (e.g. it kept inventing `window.move_window`,
# `window.move_window_to_screen`, …). Fuzzy families are handled in get() below.
_ID_ALIASES = {
    "window.move_window": "window.to_screen",
    "window.move": "window.to_screen",
    # The model invents an ABSOLUTE volume id on the system provider (which only has relative
    # volume_up/down) for "set the music to N%" — route those to the stream-targeted music control,
    # not the master sink (whose volume also carries the assistant's own voice). Live: system.set_volume_level.
    "system.set_volume_level": "tidal.set_volume",
    "system.set_volume": "tidal.set_volume",
    "media.set_volume": "tidal.set_volume",
}


def _fuzzy_alias(command_id: str) -> str | None:
    """Map a whole FAMILY of invented ids to the real one (the model riffs on names — move_window,
    move_window_to_screen, move_to_monitor, …). Conservative: only well-known hallucination shapes."""
    cid = command_id.lower()
    if cid.startswith(("window.", "desktop.")) and "move" in cid and (
            "screen" in cid or "monitor" in cid or "window" in cid or "display" in cid):
        return "window.to_screen"
    # An invented absolute-volume setter ("set"+"volume", e.g. system.set_volume_to / audio.set_volume):
    # the real relative system.volume_up/down resolve by exact match first, so only fabricated
    # absolute-set shapes reach here → the stream-level music volume.
    if "volume" in cid and "set" in cid:
        return "tidal.set_volume"
    return None


class CommandCatalog:
    def __init__(self) -> None:
        self._cmds: dict[str, Command] = {}

    def add(self, cmd: Command) -> None:
        self._cmds[cmd.id] = cmd

    def get(self, command_id: str) -> Command | None:
        c = self._cmds.get(command_id)
        if c:
            return c
        alias = _ID_ALIASES.get(command_id) or _fuzzy_alias(command_id)
        return self._cmds.get(alias) if alias else None

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
