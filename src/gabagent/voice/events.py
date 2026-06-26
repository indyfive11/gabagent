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


def convo_hold() -> VoiceEvent:
    """Conversation-hold release (brain → voice client). Emitted BEFORE `done` on a TERMINAL turn — a
    self-contained reply with no expected follow-up (one-shot Q&A, an acknowledgement, or an explicit
    dismiss) — so the voice side drops the bed-duck immediately instead of holding it the full
    conversation-hold window after an addressed reply over playing media. Arrival-keyed: the voice side
    keys on the event TYPE (a serializer dropping the literal `true` can't disarm it — the same robustness
    rule as `addressed`); `release` is carried via `extra` for logging and survives to_dict's empty-value
    filter. Safe to omit — a missing event degrades to the voice-side timed hold, so this is a pure
    optimization; the turn loop emits it conservatively (never on a media-control turn or a question)."""
    return VoiceEvent(type="convo_hold", extra={"release": True})


def voice_volume(op: str, value: float | None = None) -> VoiceEvent:
    """Change Aria's OWN TTS voice volume (brain → voice client, F3). Emitted before `done` when the user
    asks to change HER speaking volume — distinct from media/system volume, which the brain handles itself.
    `op` is up|down|set; `value` is an optional 0..1 absolute level for `set`. The voice side maps it onto
    its TTS gain (clamp [0,1]). Carried via `extra` so the wire shape is explicit and survives to_dict's
    empty-value filter (an `op`/`value` of 0 must not be stripped). Arrival-keyed like the other control
    events; safe to omit on a brain that doesn't support it. Wire shape co-designed with the voice agent."""
    extra: dict = {"op": op}
    if value is not None:
        extra["value"] = float(value)
    return VoiceEvent(type="voice_volume", extra=extra)


def timer_fired(timer_id: str, label: str = "", set_secs: int = 0) -> VoiceEvent:
    """A countdown timer (G2) came due (brain → voice client). The voice side rings on receipt and its
    stop-word cancels the ring (the LVA pattern). Carries id/label/set_secs via `extra` so the wire shape
    is explicit and survives to_dict's empty-value filter.

    NOT YET DELIVERED: the proactive brain→voice channel that carries this for an idle user is a pending
    co-design contract; until it lands the expiry ticker only dlogs the fire (see voice/timers.py)."""
    return VoiceEvent(type="timer_fired", extra={"id": timer_id, "label": label, "set_secs": int(set_secs)})


def wake_hold(hold: bool) -> VoiceEvent:
    """Confirm-style wake-window hold (brain → voice, #6 auto-Turbo offer). `hold=True` opens + FREEZES the
    command window WITH a pre-duck — so the user's "yes" spoken over playing media lands on ducked audio,
    not masked by it — and does NOT self-release. Emit `hold=False` to close it (the voice gate's max-hold
    ceiling backstops a missed release). Distinct from keepalive(), which carries `ttl_secs` and self-
    releases WITHOUT pre-ducking. Selected on the wire by the ABSENCE of ttl_secs (→ the gate's `_set_hold`
    confirm path). Carried via `extra` so a literal `hold:false` survives to_dict's empty-value filter."""
    return VoiceEvent(type="wake_hold", extra={"hold": bool(hold)})


def done() -> VoiceEvent:
    return VoiceEvent(type="done")
