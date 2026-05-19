from __future__ import annotations


class UsageTracker:
    """
    Two-tier usage tracker:
    - Arya calls are unlimited (0 credits) — not counted.
    - Premium model calls deduct from monthly credits — counted per-session as a courtesy display.
    Exact credit balance is only visible at gab.ai/settings.
    """

    def __init__(self, simple_model: str = "arya"):
        self.simple_model = simple_model
        self.premium_calls_session: int = 0
        self.arya_calls_session: int = 0

    def record(self, model: str) -> None:
        if model == self.simple_model:
            self.arya_calls_session += 1
        else:
            self.premium_calls_session += 1

    @property
    def badge(self) -> str:
        if self.premium_calls_session == 0:
            return f"{self.simple_model} ∞"
        calls = self.premium_calls_session
        noun = "call" if calls == 1 else "calls"
        return f"{self.simple_model} ∞ · {calls} premium {noun}"

    def forced_badge(self, model: str) -> str:
        if model == self.simple_model:
            return f"{model} ∞ · forced"
        calls = self.premium_calls_session
        noun = "call" if calls == 1 else "calls"
        suffix = f" · {calls} {noun}" if calls else ""
        return f"{model}{suffix} · forced"


# Keep a thin compatibility shim so existing imports don't break during transition
RateLimiter = UsageTracker
