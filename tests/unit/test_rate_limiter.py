import pytest
from gabagent.api.rate_limit import UsageTracker, RateLimiter


def test_arya_calls_not_counted_as_premium():
    tracker = UsageTracker(simple_model="arya")
    tracker.record("arya")
    tracker.record("arya")
    assert tracker.premium_calls_session == 0
    assert tracker.arya_calls_session == 2


def test_premium_calls_counted():
    tracker = UsageTracker(simple_model="arya")
    tracker.record("claude-sonnet-4.5")
    tracker.record("claude-sonnet-4.5")
    assert tracker.premium_calls_session == 2
    assert tracker.arya_calls_session == 0


def test_badge_no_premium():
    tracker = UsageTracker(simple_model="arya")
    tracker.record("arya")
    assert tracker.badge == "arya ∞"


def test_badge_one_premium_call():
    tracker = UsageTracker(simple_model="arya")
    tracker.record("claude-sonnet-4.5")
    assert tracker.badge == "arya ∞ · 1 premium call"


def test_badge_multiple_premium_calls():
    tracker = UsageTracker(simple_model="arya")
    tracker.record("claude-sonnet-4.5")
    tracker.record("claude-sonnet-4.5")
    tracker.record("claude-sonnet-4.5")
    assert tracker.badge == "arya ∞ · 3 premium calls"


def test_forced_badge_simple_model():
    tracker = UsageTracker(simple_model="arya")
    assert tracker.forced_badge("arya") == "arya ∞ · forced"


def test_forced_badge_premium_no_calls():
    tracker = UsageTracker(simple_model="arya")
    assert tracker.forced_badge("claude-sonnet-4.5") == "claude-sonnet-4.5 · forced"


def test_forced_badge_premium_with_calls():
    tracker = UsageTracker(simple_model="arya")
    tracker.record("claude-sonnet-4.5")
    tracker.record("claude-sonnet-4.5")
    assert tracker.forced_badge("claude-sonnet-4.5") == "claude-sonnet-4.5 · 2 calls · forced"


def test_rate_limiter_shim_is_usage_tracker():
    assert RateLimiter is UsageTracker
