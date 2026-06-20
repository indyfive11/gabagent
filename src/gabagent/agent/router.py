from __future__ import annotations
from typing import TYPE_CHECKING
from gabagent.api.models import ChatMessage
from gabagent.tui.renderer import console

if TYPE_CHECKING:
    from gabagent.api.client import GabAIClient
    from gabagent.config.models import GabAgentConfig, Rung

_ROUTING_PROMPT = """\
You are a model router. Classify the complexity of the user's request.
Return ONLY one of these two tags — no explanation, no other text:
[SIMPLE] — file reading, searching, git commands, simple questions, summarization, small edits
[COMPLEX] — writing new classes, multi-file refactoring, complex debugging, architecture changes

User request: {prompt}"""

# Per-turn rung classifier (Claude ladder). The model rates the turn 0..N where N = top rung index;
# we parse the first integer it returns. Bias toward rounding up is applied in classify_rung.
_RUNG_PROMPT = """\
You are an effort router for an AI coding assistant. Rate how much model capability and reasoning
effort the user's request needs, on a scale from 0 (trivial) to {top} (hardest):

0 — trivial: greetings, a one-line factual question, a tiny lookup
1 — easy: read/search a file, run a simple command, summarize a short snippet
2 — moderate: a small focused edit, explain some code, a single-file change
{mid_lines}
{top} — hardest: large multi-file refactor, subtle concurrency/architecture work, deep debugging

Return ONLY the single integer for the right rung — no words, no punctuation.

User request: {prompt}"""


class ModelRouter:
    def __init__(
        self,
        config: GabAgentConfig,
        *,
        ladder: list[Rung] | None = None,
        classifier_model: str | None = None,
        classifier_effort: str | None = None,
        classifier_backend: str = "claude",
        assembled: bool = False,
    ) -> None:
        self.enabled = config.router.enabled
        self.classifier_enabled = config.router.classifier_enabled
        self.simple_model = config.router.simple_model
        self.complex_model = config.router.complex_model
        self.provider = config.provider
        # Default (back-compat): the raw Claude-only ladder, classified on its bottom rung. The
        # cross-backend assembled ladder is opt-in via ModelRouter.assemble(...).
        self.ladder: list[Rung] = ladder if ladder is not None else list(config.claude.ladder)
        self.assembled = assembled
        b0 = self.ladder[0] if self.ladder else None
        self.classifier_model = classifier_model or (b0.model if b0 else self.simple_model)
        self.classifier_effort = (
            classifier_effort if classifier_effort is not None else (b0.effort if b0 else "")
        )
        self.classifier_backend = classifier_backend

    @classmethod
    def assemble(
        cls,
        config: GabAgentConfig,
        *,
        local_floor: bool = False,
        local_running: bool = False,
        degraded: set | None = None,
    ) -> "ModelRouter | None":
        """Build a UNIFIED cross-backend ladder: [local? → Aria(gab)? → Claude rungs?].

        Returns None when assembly adds nothing over the legacy provider path (no local floor and
        no real Anthropic rungs) — the caller then falls through to today's per-provider routing.
        The classifier is pinned to a fixed cheap CLOUD model (haiku if Anthropic is configured,
        else Aria) so it never runs on the unreliable local rung. `degraded` backends (hard-failed
        this session — billing/auth) are SKIPPED so the floor moves up off a dead backend.
        """
        from gabagent.api.factory import anthropic_configured
        from gabagent.config.models import Rung

        degraded = degraded or set()
        include_local = bool(local_floor and local_running and config.local_model) and "local" not in degraded
        include_gab = bool(config.api_key) and "gab" not in degraded  # Aria reachable on gab.ai
        include_claude = anthropic_configured(config) and (
            config.router.cross_backend or config.provider == "claude"
        ) and "claude" not in degraded
        # Assembly only earns its keep when there's a rung below Aria (local) or above it (Claude).
        if not (include_local or include_claude):
            return None

        rungs: list[Rung] = []
        if include_local:
            rungs.append(Rung(model=config.local_model, effort="", backend="local"))
        if include_gab:
            rungs.append(Rung(model=config.router.simple_model, effort="", backend="gab"))
        if include_claude:
            rungs.extend(config.claude.ladder)  # each already backend="claude"
        if not rungs:  # defensive — should not happen given the guard above
            return None

        if include_claude:
            c0 = config.claude.ladder[0]
            cmodel, ceffort, cbackend = c0.model, c0.effort, "claude"
        else:
            cmodel, ceffort, cbackend = config.router.simple_model, "", "gab"
        return cls(
            config,
            ladder=rungs,
            classifier_model=cmodel,
            classifier_effort=ceffort,
            classifier_backend=cbackend,
            assembled=True,
        )

    # -- gab 2-tier path (unchanged) ---------------------------------------

    async def classify_intent(self, prompt: str, client: GabAIClient) -> str:
        if not self.classifier_enabled:
            return self.simple_model
        try:
            messages = [ChatMessage(role="user", content=_ROUTING_PROMPT.format(prompt=prompt))]
            tag = await client.complete_simple(messages, model=self.simple_model)
            model = self.simple_model if "[SIMPLE]" in tag else self.complex_model
            if model != self.simple_model:
                console.print(f"[gab.accent]▸[/gab.accent] [dim]routing → {model}[/dim]", markup=True)
            return model
        except Exception:
            return self.simple_model

    # -- Claude ladder path ------------------------------------------------

    @property
    def top_rung(self) -> int:
        return len(self.ladder) - 1

    def rung(self, idx: int) -> Rung:
        idx = max(0, min(idx, self.top_rung))
        return self.ladder[idx]

    def index_of(self, model: str | None, effort: str | None, backend: str | None) -> int | None:
        """Ladder index of the rung currently active, or None when it isn't on the ladder (e.g. a
        legacy/pinned model). Used by the reactive sequential climb to know where it stands."""
        for i, r in enumerate(self.ladder):
            if r.model == model and (r.effort or None) == (effort or None) and r.backend == backend:
                return i
        return None

    def next_rung(self, model: str | None, effort: str | None, backend: str | None) -> Rung | None:
        """The next rung UP from the active one — sequential, least-escalation-first (+1, not a jump
        to the top). None when already at the ceiling or the active rung isn't on the ladder. This is
        the climb the loop-detector takes: start at the floor, step up only on evidence of trouble."""
        i = self.index_of(model, effort, backend)
        if i is None or i >= self.top_rung:
            return None
        return self.ladder[i + 1]

    async def classify_rung(self, prompt: str, client: GabAIClient) -> int:
        """Pick a ladder rung index for a fresh user turn. Bottom rung when the classifier is off;
        on genuine ambiguity (unparseable reply) round UP one rung — correctness over a few cents."""
        if not self.classifier_enabled or self.top_rung <= 0:
            return 0
        top = self.top_rung
        # Fill the middle of the scale so the prompt names every rung between 2 and top-1.
        mid_lines = "\n".join(
            f"{i} — increasingly hard" for i in range(3, top)
        ) if top > 3 else ""
        try:
            messages = [ChatMessage(role="user", content=_RUNG_PROMPT.format(
                top=top, mid_lines=mid_lines, prompt=prompt,
            ))]
            # Classify on a fixed cheap CLOUD model (never the local floor rung).
            reply = await client.complete_simple(
                messages, model=self.classifier_model, effort=self.classifier_effort or None
            )
            idx = _first_int(reply)
            if idx is None:
                idx = min(1, top)  # ambiguous → one rung up from bottom
            idx = max(0, min(idx, top))
            if idx > 0:
                r = self.ladder[idx]
                eff = f"/{r.effort}" if r.effort else ""
                console.print(f"[gab.accent]▸[/gab.accent] [dim]routing → {r.model}{eff}[/dim]", markup=True)
            return idx
        except Exception:
            return 0

    def reactive_min_rung(self, tool_name: str) -> int | None:
        """A write/edit/commit tool floors the turn at the first opus rung (mirrors the gab path's
        'any file modification uses the complex model'). Returns None when no floor applies."""
        if tool_name in ("write_file", "edit", "git_commit"):
            for i, r in enumerate(self.ladder):
                if r.model.lower().startswith("claude-opus"):
                    return i
            return self.top_rung
        return None

    # -- gab back-compat reactive (unchanged behavior) ---------------------

    def check_tool_complexity(self, tool_name: str, args: dict) -> str | None:
        # Any file modification always uses the complex model — no threshold.
        # Arya stays on read-only work (grep, glob, read_file, bash queries).
        if tool_name in ("write_file", "edit", "git_commit"):
            return self.complex_model
        return None

    def check_reactive(
        self, tool_name: str, exit_code: int | None, active_model: str | None
    ) -> str | None:
        # Disabled: bash exit code 1 is normal for grep/find with no matches,
        # and caused runaway escalation to the paid model during routine searches.
        return None


def _first_int(s: str) -> int | None:
    import re
    m = re.search(r"-?\d+", s or "")
    return int(m.group()) if m else None


# Device/media/system CONTROL intent — turns that drive run_command. A weak local model reliably
# fumbles these (e.g. it called run_command with command_id="run_command" trying to lower the music),
# so when the floor is local we route them up to Aria (which handles tools well). Conversation stays
# local. Bias to catching commands: a false positive just sends a chat turn one rung up (cheap); a
# miss sends a command to a model that can't run it (the failure we're fixing).
import re as _re
_COMMAND_INTENT = _re.compile(
    r"\bturn\s+(?:it|the|that|them)?\s*(?:up|down|on|off)\b"
    r"|\b(?:volume|louder|quieter|softer|mute|unmute|un-?pause)\b"
    r"|\b(?:pause|resume|skip|rewind|fast.?forward)\b"
    r"|\b(?:next|previous|last)\s+(?:track|song|one)\b"
    r"|\b(?:play|stop|pause|put\s+on|queue)\b.{0,30}\b(?:music|song|track|playlist|movie|show|video|album|tidal|jellyfin)\b"
    r"|\b(?:play|put\s+on|queue(?:\s+up)?)\s+(?:some|the|a|an|me|us|my|that|something)?\s*\w+"
    r"|\b(?:music|movie|show|video|playlist|song|volume)\b.{0,20}\b(?:up|down|off|on|louder|quieter|paused?|stop|play)\b"
    r"|\b(?:open|launch|close|quit)\s+(?:the\s+)?\w+"
    r"|\b(?:brightness|dim|brighten)\b"
    r"|\b(?:set|change|lower|raise|increase|decrease|crank|bump)\b.{0,25}\b(?:volume|brightness|music|temperature|lights?)\b",
    _re.I,
)


def looks_like_command(text: str) -> bool:
    """True when the utterance is a device/media/system CONTROL request (drives run_command)."""
    return bool(_COMMAND_INTENT.search(text or ""))


_PIN_BASE = {
    "opus": ("claude-opus-4-8", "high"),
    "sonnet": ("claude-sonnet-4-6", "high"),
    "haiku": ("claude-haiku-4-5", ""),
}
_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


def resolve_pin(text: str, config: "GabAgentConfig") -> "tuple[str, str | None, str] | None":
    """Resolve a spoken/typed model name (alias or exact id, optional effort) to a
    (model, effort, backend) pin, or None if unknown. Aliases: opus/sonnet/haiku → Claude;
    aria/arya/gab → Aria; local/devstral → the local model. e.g. "opus max" → opus at max effort."""
    parts = (text or "").lower().split()
    if not parts:
        return None
    name = parts[0]
    effort_override = next((p for p in parts[1:] if p in _EFFORTS), None)
    if name in ("aria", "arya", "gab"):
        return (config.router.simple_model, None, "gab")
    if name in ("local", "devstral") or (config.local_model and name == config.local_model.lower()):
        return (config.local_model, None, "local") if config.local_model else None
    if name in _PIN_BASE:
        model, eff = _PIN_BASE[name]
        return (model, effort_override or eff, "claude")
    if name.startswith("claude-"):
        return (name, effort_override or "high", "claude")
    if name == config.router.simple_model.lower():
        return (config.router.simple_model, None, "gab")
    for r in config.claude.ladder:
        if r.model.lower() == name:
            return (r.model, effort_override or (r.effort or None), "claude")
    return None
