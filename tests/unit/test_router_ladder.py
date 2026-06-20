import types
import pytest

from gabagent.agent.router import ModelRouter, _first_int
from gabagent.config.models import GabAgentConfig


class _FakeClient:
    """complete_simple returns a canned reply (the classifier's integer)."""
    def __init__(self, reply):
        self.reply = reply
        self.last_model = None
        self.last_effort = None

    async def complete_simple(self, messages, model=None, effort=None):
        self.last_model = model
        self.last_effort = effort
        return self.reply


def _router(**router_cfg):
    cfg = GabAgentConfig()
    cfg.provider = "claude"
    for k, v in router_cfg.items():
        setattr(cfg.router, k, v)
    return ModelRouter(cfg), cfg


def test_first_int():
    assert _first_int("3") == 3
    assert _first_int("rung 4 please") == 4
    assert _first_int("nope") is None


def test_index_of_and_next_rung_climb_sequentially():
    # The loop-detector's sequential climb: from any rung, next_rung steps +1 (least escalation first),
    # not a jump to the top. Walk the whole ladder a rung at a time and stop at the ceiling.
    r, _ = _router()
    ladder = r.ladder
    assert len(ladder) >= 2
    for i, rung in enumerate(ladder):
        assert r.index_of(rung.model, rung.effort or None, rung.backend) == i
        nxt = r.next_rung(rung.model, rung.effort or None, rung.backend)
        if i < r.top_rung:
            assert (nxt.model, nxt.effort or None, nxt.backend) == (
                ladder[i + 1].model, ladder[i + 1].effort or None, ladder[i + 1].backend)
        else:
            assert nxt is None          # at the ceiling there's nowhere higher to climb


def test_next_rung_off_ladder_is_none():
    # A pinned/legacy model that isn't on the ladder has no defined climb.
    r, _ = _router()
    assert r.index_of("some-unknown-model", None, "gab") is None
    assert r.next_rung("some-unknown-model", None, "gab") is None


@pytest.mark.asyncio
async def test_trivial_maps_to_bottom():
    r, _ = _router()
    idx = await r.classify_rung("hi", _FakeClient("0"))
    assert idx == 0


@pytest.mark.asyncio
async def test_hard_maps_to_top():
    r, _ = _router()
    top = r.top_rung
    idx = await r.classify_rung("rewrite everything", _FakeClient(str(top)))
    assert idx == top


@pytest.mark.asyncio
async def test_overshoot_clamped_to_top():
    r, _ = _router()
    idx = await r.classify_rung("x", _FakeClient("999"))
    assert idx == r.top_rung


@pytest.mark.asyncio
async def test_ambiguous_rounds_up():
    r, _ = _router()
    idx = await r.classify_rung("x", _FakeClient("not a number"))
    assert idx == 1  # one rung above bottom


@pytest.mark.asyncio
async def test_classifier_disabled_pins_bottom():
    r, _ = _router(classifier_enabled=False)
    idx = await r.classify_rung("anything hard", _FakeClient("4"))
    assert idx == 0


@pytest.mark.asyncio
async def test_classifier_uses_bottom_rung_model():
    r, _ = _router()
    fc = _FakeClient("2")
    await r.classify_rung("x", fc)
    assert fc.last_model == "claude-haiku-4-5"


def test_reactive_write_tool_floors_at_opus():
    r, cfg = _router()
    floor = r.reactive_min_rung("write_file")
    assert floor is not None
    assert r.rung(floor).model.startswith("claude-opus")
    # first opus rung in the default ladder is index 3
    assert floor == 3


def test_reactive_readonly_tool_no_floor():
    r, _ = _router()
    assert r.reactive_min_rung("read_file") is None


def test_rung_clamps_out_of_range():
    r, _ = _router()
    assert r.rung(-5) is r.ladder[0]
    assert r.rung(999) is r.ladder[r.top_rung]
