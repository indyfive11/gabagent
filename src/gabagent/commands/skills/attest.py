"""LLM attestation of a skill manifest — configurable reviewer, default Claude API.

The reviewer reads the skill's declared commands/backends and returns a per-command tier +
danger verdict + a plain-English risk explanation for the user. It is ADVISORY: it can only
raise a command's tier (combined via max() with the self-declared tier and the static floor),
never lower it, and the runtime gate still enforces the resulting tier on every call.
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from gabagent.api.models import ChatMessage

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext
    from gabagent.commands.skills.loader import SkillManifest

_PROMPT = """You are a security reviewer for a voice-assistant SKILL PLUGIN. The skill declares \
commands that the assistant may run by voice. Each command has a backend describing exactly what \
it executes (a shell argv list, an HTTP call, or an app launch). Assess each command for risk.

Assign each command a tier:
  1 = safe read/transport (queries, media play/pause, harmless info)
  2 = scoped & reversible (launch an app, open a URL, a local non-system change)
  3 = system-altering, irreversible, privileged, destructive, network-exfil, or DECEPTIVE/OBFUSCATED
Set "dangerous": true for anything in tier 3 or anything that hides its true effect (e.g. base64,
shell wrappers, fetch-and-execute, a benign name masking a destructive command).

Return ONLY a JSON object, no prose:
{"commands": {"<command_id>": {"tier": 1|2|3, "dangerous": bool, "rationale": "<short>"}},
 "overall": "low|medium|high", "explanation": "<one or two plain sentences for the user>"}

Skill: {skill}
Commands:
{commands}
"""


@dataclass
class AttestationVerdict:
    per: dict = field(default_factory=dict)   # cmd_id -> {tier, dangerous, rationale}
    overall: str = "unknown"
    explanation: str = ""

    def tier(self, cid: str) -> int:
        return int(self.per.get(cid, {}).get("tier", 3))

    def dangerous(self, cid: str) -> bool:
        return bool(self.per.get(cid, {}).get("dangerous", True))


def _describe(cmd) -> str:
    b = cmd.backend
    k = getattr(b, "kind", "")
    if k == "shell":
        detail = "argv=" + json.dumps(b.argv)
    elif k == "http":
        detail = f"{b.method} {b.path} query={b.query} auth={b.auth!r}"
    elif k == "launch":
        detail = f"target={b.target!r}"
    else:
        detail = k
    return f"- {cmd.id} (declared tier {cmd.tier}): {cmd.summary} | backend[{k}]: {detail}"


def _all_tier3(manifest, explanation: str) -> AttestationVerdict:
    per = {c.id: {"tier": 3, "dangerous": True, "rationale": "fail-closed"} for c in manifest.commands}
    return AttestationVerdict(per=per, overall="high", explanation=explanation)


class NullAttestor:
    """reviewer='off' — no analysis; everything floored to tier 3 (fail-closed)."""
    async def attest(self, manifest, ctx) -> AttestationVerdict:
        return _all_tier3(manifest, "Attestation is disabled; every command is floored to keyboard confirmation.")


class ClaudeApiAttestor:
    def __init__(self, model: str = ""):
        self.model = model

    async def attest(self, manifest, ctx) -> AttestationVerdict:
        model = self.model or ctx.config.router.complex_model
        prompt = _PROMPT.format(
            skill=f"{manifest.id} — {manifest.name}: {manifest.description}",
            commands="\n".join(_describe(c) for c in manifest.commands),
        )
        try:
            raw = await ctx.client.complete_simple([ChatMessage(role="user", content=prompt)], model=model)
            data = _extract_json(raw)
        except Exception:
            return _all_tier3(manifest, "Attestation review failed; treating all commands as high-risk.")
        per = {}
        for c in manifest.commands:
            entry = (data.get("commands") or {}).get(c.id, {})
            per[c.id] = {
                "tier": int(entry.get("tier", 3)) if entry.get("tier") in (1, 2, 3) else 3,
                "dangerous": bool(entry.get("dangerous", True)),
                "rationale": str(entry.get("rationale", "")),
            }
        return AttestationVerdict(per=per, overall=str(data.get("overall", "unknown")),
                                  explanation=str(data.get("explanation", "")))


class ClaudeCodeBridgeAttestor:
    """reviewer='claude_code_bridge' — route to the Claude Code instance via the bridge
    for a human-in-the-loop review. Async; not implemented in Phase 1 — falls back to API."""
    async def attest(self, manifest, ctx) -> AttestationVerdict:
        return await ClaudeApiAttestor().attest(manifest, ctx)


def get_attestor(cfg):
    reviewer = getattr(cfg, "reviewer", "claude_api")
    if reviewer == "off":
        return NullAttestor()
    if reviewer == "claude_code_bridge":
        return ClaudeCodeBridgeAttestor()
    return ClaudeApiAttestor(model=getattr(cfg, "model", ""))


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    if m:
        raw = m.group(1)
    else:
        s, e = raw.find("{"), raw.rfind("}")
        if s != -1 and e != -1:
            raw = raw[s:e + 1]
    return json.loads(raw)
