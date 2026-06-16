"""Events streamed from a voice turn to the brain-protocol client over SSE."""
from __future__ import annotations
import json
from dataclasses import dataclass, asdict


@dataclass
class VoiceEvent:
    type: str            # token | status | confirm | blocked | error | done
    text: str = ""
    id: str = ""
    tier: int = 0
    method: str = ""     # spoken_yesno | keyboard | passphrase | ""
    summary: str = ""
    reason: str = ""
    action: str = ""
    prompt_is_complete: bool = False   # confirm: summary is the full spoken line (incl. its own yes/no)
    extra: dict | None = None          # explicit payload merged verbatim — escapes the empty-value filter
                                       # below (needed for a bool `false`, since False in ("", 0) is True)

    def to_dict(self) -> dict:
        raw = asdict(self)
        extra = raw.pop("extra", None) or {}
        d = {k: v for k, v in raw.items() if v not in ("", 0)}
        d["type"] = self.type
        d.update(extra)
        return d

    def sse(self) -> str:
        """Serialize as a Server-Sent Events `data:` frame."""
        return f"data: {json.dumps(self.to_dict())}\n\n"


# Convenience constructors -------------------------------------------------

def token(text: str) -> VoiceEvent:
    return VoiceEvent(type="token", text=text)


def status(text: str) -> VoiceEvent:
    return VoiceEvent(type="status", text=text)


def confirm(cid: str, tier: int, method: str, summary: str, reason: str = "",
            prompt_complete: bool = False) -> VoiceEvent:
    """A gate confirm. By convention `summary` is a bare action/question and the voice
    client appends the yes/no instruction (Option A). Set `prompt_complete` when the
    summary already contains its own choice (e.g. a 'use it / open a new window' surface)
    so the client speaks it verbatim and appends nothing."""
    return VoiceEvent(type="confirm", id=cid, tier=tier, method=method, summary=summary,
                      reason=reason, prompt_is_complete=prompt_complete)


def blocked(action: str, reason: str) -> VoiceEvent:
    return VoiceEvent(type="blocked", action=action, reason=reason)


def error(cause: str, text: str = "") -> VoiceEvent:
    """A turn-level failure. `text` is speakable; `summary` carries the structured cause."""
    return VoiceEvent(type="error", text=text or "Sorry, I hit a problem.", summary=cause)


def addressed(value: bool) -> VoiceEvent:
    """Signal the addressed-classifier verdict to the voice client (A1 movie-duck release).

    Emitted only on the SUPPRESSION path (an aside) so the client can release a movie duck that its
    VAD-onset pre-duck opened for speech that turned out not to be addressed to the assistant — instead
    of letting it linger until the voice-side 8s idle grace. The client treats `addressed:true` as a
    no-op, so we never emit that. Carries an explicit `addressed` bool via `extra` (a plain bool field
    would be stripped by to_dict's empty-value filter, since `False == 0`)."""
    return VoiceEvent(type="addressed", extra={"addressed": bool(value)})


def keepalive(ttl_secs: int) -> VoiceEvent:
    """Media-control keepalive (brain → voice client). Asks the client to hold the wake/command window
    open for `ttl_secs` longer so a follow-up media command needs no re-wake while music plays. Emitted
    after a media command runs and refreshed per command; the client (re)arms a TTL timer on each and
    releases on expiry — its own max-hold ceiling backstops a missed refresh. Carries `hold`/`ttl_secs`
    via `extra` so the wire shape is explicit and survives to_dict's empty-value filter."""
    return VoiceEvent(type="wake_hold", extra={"hold": True, "ttl_secs": int(ttl_secs)})


def timer_fired(timer_id: str, label: str = "", set_secs: int = 0) -> VoiceEvent:
    """A countdown timer (G2) came due (brain → voice client). The voice side rings on receipt and its
    stop-word cancels the ring (the LVA pattern). Carries id/label/set_secs via `extra` so the wire shape
    is explicit and survives to_dict's empty-value filter.

    NOT YET DELIVERED: the proactive brain→voice channel that carries this for an idle user is a pending
    co-design contract; until it lands the expiry ticker only dlogs the fire (see voice/timers.py)."""
    return VoiceEvent(type="timer_fired", extra={"id": timer_id, "label": label, "set_secs": int(set_secs)})


def done() -> VoiceEvent:
    return VoiceEvent(type="done")
