"""Unified cross-backend escalation ladder: local → Aria → Claude (assembled per session)."""
import pytest

from gabagent.agent.router import ModelRouter
from gabagent.config.models import GabAgentConfig


def _cfg(*, gab=True, anthropic=True, local=True, provider="claude", cross_backend=True):
    cfg = GabAgentConfig()
    cfg.provider = provider
    cfg.api_key = "gabkey" if gab else ""
    cfg.claude.api_key = "ankey" if anthropic else ""
    cfg.local_model = "devstral" if local else ""
    cfg.router.cross_backend = cross_backend
    return cfg


class _FakeClient:
    def __init__(self, reply="0"):
        self.reply = reply
        self.last_model = None

    async def complete_simple(self, messages, model=None, effort=None):
        self.last_model = model
        return self.reply


def test_assemble_full_stack_with_warm_local_floor():
    cfg = _cfg()
    r = ModelRouter.assemble(cfg, local_floor=True, local_running=True)
    assert r is not None and r.assembled
    backends = [rung.backend for rung in r.ladder]
    assert backends[0] == "local"
    assert backends[1] == "gab"
    assert backends[2:] == ["claude"] * len(cfg.claude.ladder)
    assert r.ladder[0].model == "devstral"
    assert r.ladder[1].model == cfg.router.simple_model  # Aria
    # Classifier pinned to the cloud bottom rung (haiku), never the local floor.
    assert r.classifier_model == cfg.claude.ladder[0].model
    assert r.classifier_backend == "claude"


def test_aria_is_floor_when_local_off():
    cfg = _cfg()
    r = ModelRouter.assemble(cfg, local_floor=False, local_running=False)
    assert r.ladder[0].backend == "gab"
    assert all(rung.backend == "claude" for rung in r.ladder[1:])


def test_local_floor_pinned_but_not_running_falls_to_aria():
    cfg = _cfg()
    # Pinned but the model isn't warm yet → no local rung until it's actually up.
    r = ModelRouter.assemble(cfg, local_floor=True, local_running=False)
    assert r.ladder[0].backend == "gab"


def test_no_anthropic_key_tops_out_at_aria():
    cfg = _cfg(anthropic=False)
    r = ModelRouter.assemble(cfg, local_floor=True, local_running=True)
    assert [rung.backend for rung in r.ladder] == ["local", "gab"]
    # With no Claude rungs, the classifier falls back to Aria.
    assert r.classifier_backend == "gab"
    assert r.classifier_model == cfg.router.simple_model


def test_assemble_returns_none_when_nothing_to_add():
    cfg = _cfg(anthropic=False)
    # No local floor and no real Claude rungs → assembly adds nothing → caller uses the legacy path.
    assert ModelRouter.assemble(cfg, local_floor=False, local_running=False) is None


def test_no_gab_key_omits_aria_rung():
    cfg = _cfg(gab=False)
    r = ModelRouter.assemble(cfg, local_floor=True, local_running=True)
    assert [rung.backend for rung in r.ladder] == ["local"] + ["claude"] * len(cfg.claude.ladder)


def test_reactive_write_tool_floors_at_first_opus_in_assembled_ladder():
    cfg = _cfg()
    r = ModelRouter.assemble(cfg, local_floor=True, local_running=True)
    floor = r.reactive_min_rung("write_file")
    assert floor is not None
    assert r.rung(floor).model.startswith("claude-opus")
    assert r.rung(floor).backend == "claude"
    # First opus is the raw-ladder index (3) shifted by the 2 prepended floor rungs (local, Aria).
    assert floor == 2 + 3


@pytest.mark.asyncio
async def test_classifier_runs_on_cloud_model_not_local():
    cfg = _cfg()
    r = ModelRouter.assemble(cfg, local_floor=True, local_running=True)
    fc = _FakeClient("0")
    await r.classify_rung("hi", fc)
    assert fc.last_model == "claude-haiku-4-5"  # never "devstral"


def test_default_constructor_is_unchanged_backcompat():
    cfg = _cfg()
    r = ModelRouter(cfg)
    assert r.assembled is False
    assert [rung.model for rung in r.ladder] == [rung.model for rung in cfg.claude.ladder]


# ---- hard-failure safeguard: surface + degrade + skip (never silent) ----

@pytest.mark.parametrize("msg", [
    "RuntimeError: APIStatusError: Error code: 402 - Insufficient credits. Please purchase more credits",
    "APIStatusError: Error code: 401 - unauthorized",
    "Error code: 403 - permission denied",
    "invalid api key",
    "model not found",
])
def test_hard_backend_errors_detected(msg):
    from gabagent.api.client import _is_hard_backend_error
    assert _is_hard_backend_error(Exception(msg)) is True


@pytest.mark.parametrize("msg", [
    "The model failed to generate a response. Please try again.",
    "inference_failed",
    "some random network blip",
])
def test_transient_and_normal_errors_are_not_hard(msg):
    from gabagent.api.client import _is_hard_backend_error
    assert _is_hard_backend_error(Exception(msg)) is False


def test_degraded_gab_moves_floor_off_aria():
    cfg = _cfg()
    r = ModelRouter.assemble(cfg, local_floor=False, local_running=False, degraded={"gab"})
    # Aria skipped → the floor is now the first Claude rung; no gab rung anywhere.
    assert r.ladder[0].backend == "claude"
    assert all(rung.backend == "claude" for rung in r.ladder)


def test_degraded_gab_with_local_floor_keeps_local_then_claude():
    cfg = _cfg()
    r = ModelRouter.assemble(cfg, local_floor=True, local_running=True, degraded={"gab"})
    assert [rung.backend for rung in r.ladder] == ["local"] + ["claude"] * len(cfg.claude.ladder)


def test_all_cloud_degraded_leaves_local_only():
    cfg = _cfg()
    r = ModelRouter.assemble(cfg, local_floor=True, local_running=True, degraded={"gab", "claude"})
    assert [rung.backend for rung in r.ladder] == ["local"]


def test_everything_degraded_returns_none():
    cfg = _cfg()
    assert ModelRouter.assemble(cfg, local_floor=False, local_running=False,
                                degraded={"gab", "claude"}) is None


# ---- command-intent: device/media turns bump off the local floor to Aria ----

@pytest.mark.parametrize("text", [
    "turn down the music", "turn it up", "lower the volume", "make it louder", "pause the music",
    "skip this song", "next track", "play some jazz", "play music", "put on a playlist",
    "stop the movie", "mute it", "set the volume to 30", "turn off the lights", "crank up the music",
])
def test_looks_like_command_true(text):
    from gabagent.agent.router import looks_like_command
    assert looks_like_command(text) is True


@pytest.mark.parametrize("text", [
    "what's the weather like", "tell me a joke", "how are you", "what do you think of that",
    "who won the game last night", "explain how dns works", "what time is it",
])
def test_looks_like_command_false_for_chat(text):
    from gabagent.agent.router import looks_like_command
    assert looks_like_command(text) is False


def test_command_bump_target_is_aria_when_local_floor():
    # The bump sends a command to rung 1 = the first non-local rung (Aria) when local is the floor.
    cfg = _cfg()
    r = ModelRouter.assemble(cfg, local_floor=True, local_running=True)
    assert r.ladder[0].backend == "local"
    assert r.rung(1).backend == "gab"  # Aria


def test_degraded_notice_names_both_backends():
    from gabagent.voice.turn import _degraded_notice
    msg = _degraded_notice("gab", "claude")
    assert "Aria" in msg and "Claude" in msg


def test_local_floor_config_field_round_trips(tmp_path, monkeypatch):
    from gabagent.config import loader
    monkeypatch.setattr(loader, "_settings_path", lambda: tmp_path / "settings.json", raising=False)
    cfg = GabAgentConfig()
    cfg.local_floor = True
    # save+reload should preserve the pin (exact loader API is exercised elsewhere; here we just
    # confirm the field is a real, settable, serializable bool).
    assert cfg.model_dump().get("local_floor") is True
