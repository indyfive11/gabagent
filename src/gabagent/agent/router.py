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
    def __init__(self, config: GabAgentConfig) -> None:
        self.enabled = config.router.enabled
        self.classifier_enabled = config.router.classifier_enabled
        self.simple_model = config.router.simple_model
        self.complex_model = config.router.complex_model
        self.provider = config.provider
        self.ladder: list[Rung] = list(config.claude.ladder)

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
        bottom = self.ladder[0]
        try:
            messages = [ChatMessage(role="user", content=_RUNG_PROMPT.format(
                top=top, mid_lines=mid_lines, prompt=prompt,
            ))]
            reply = await client.complete_simple(messages, model=bottom.model, effort=bottom.effort or None)
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
