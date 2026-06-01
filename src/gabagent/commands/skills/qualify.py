"""The qualify pipeline + install/record: validate → static scan → attest → effective tier
→ (user decision) → hash-bound record. Effective tier = max(declared, static_floor, attested);
attestation can RAISE but never lower. The runtime gate still enforces it on every call.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from gabagent.commands.skills.loader import SkillManifest, manifest_hash, skills_root
from gabagent.commands.skills.staticscan import static_scan, StaticVerdict
from gabagent.commands.skills.attest import get_attestor, AttestationVerdict

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


@dataclass
class Qualification:
    skill_id: str
    rejected: bool = False
    reason: str = ""
    effective: dict = field(default_factory=dict)   # cmd_id -> effective tier
    dangerous: bool = False
    explanation: str = ""
    flags: list[str] = field(default_factory=list)
    static: StaticVerdict | None = None
    attest: AttestationVerdict | None = None


async def qualify_skill(manifest: SkillManifest, ctx: AgentContext) -> Qualification:
    cfg = getattr(ctx.config, "attestation", None)
    auto_reject = bool(getattr(cfg, "auto_reject_obfuscation", False))

    sv = static_scan(manifest.commands, auto_reject_obfuscation=auto_reject)
    if sv.reject:
        return Qualification(skill_id=manifest.id, rejected=True, reason=sv.reject_reason,
                             static=sv, flags=sv.all_flags(), dangerous=True,
                             explanation="Rejected by the static scan; declarative skills must be transparent.")

    av = await get_attestor(cfg).attest(manifest, ctx)

    effective, dangerous = {}, False
    for c in manifest.commands:
        eff = max(c.tier, sv.floor(c.id), av.tier(c.id))
        effective[c.id] = eff
        if eff >= 3 or av.dangerous(c.id):
            dangerous = True

    return Qualification(
        skill_id=manifest.id, rejected=False, effective=effective, dangerous=dangerous,
        explanation=av.explanation, flags=sv.all_flags(), static=sv, attest=av,
    )


def record_path(skill_id: str) -> Path:
    return skills_root() / skill_id / "attestation.json"


def write_record(manifest: SkillManifest, qual: Qualification, approved: bool, enabled: bool) -> Path:
    d = skills_root() / manifest.id
    d.mkdir(parents=True, exist_ok=True)
    (d / "skill.toml").write_bytes(manifest.raw_bytes)
    record = {
        "skill_id": manifest.id,
        "name": manifest.name,
        "manifest_hash": manifest_hash(manifest.raw_bytes),
        "effective_tiers": qual.effective,
        "dangerous": qual.dangerous,
        "explanation": qual.explanation,
        "flags": qual.flags,
        "user_decision": "approved" if approved else "declined",
        "enabled": enabled,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }
    (d / "attestation.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    return d


def list_installed() -> list[dict]:
    base = skills_root()
    if not base.exists():
        return []
    out = []
    for d in sorted(base.iterdir()):
        rf = d / "attestation.json"
        if rf.exists():
            try:
                out.append(json.loads(rf.read_text(encoding="utf-8")))
            except Exception:
                pass
    return out
