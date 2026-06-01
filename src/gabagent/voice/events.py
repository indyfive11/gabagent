"""Events streamed from a voice turn to the brain-protocol client over SSE."""
from __future__ import annotations
import json
from dataclasses import dataclass, asdict


@dataclass
class VoiceEvent:
    type: str            # token | status | confirm | blocked | done
    text: str = ""
    id: str = ""
    tier: int = 0
    method: str = ""     # spoken_yesno | keyboard | passphrase | ""
    summary: str = ""
    reason: str = ""
    action: str = ""

    def to_dict(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if v not in ("", 0)}
        d["type"] = self.type
        return d

    def sse(self) -> str:
        """Serialize as a Server-Sent Events `data:` frame."""
        return f"data: {json.dumps(self.to_dict())}\n\n"


# Convenience constructors -------------------------------------------------

def token(text: str) -> VoiceEvent:
    return VoiceEvent(type="token", text=text)


def status(text: str) -> VoiceEvent:
    return VoiceEvent(type="status", text=text)


def confirm(cid: str, tier: int, method: str, summary: str, reason: str = "") -> VoiceEvent:
    return VoiceEvent(type="confirm", id=cid, tier=tier, method=method, summary=summary, reason=reason)


def blocked(action: str, reason: str) -> VoiceEvent:
    return VoiceEvent(type="blocked", action=action, reason=reason)


def done() -> VoiceEvent:
    return VoiceEvent(type="done")
