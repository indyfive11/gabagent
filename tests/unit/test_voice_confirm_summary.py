"""Consequential (Tier 2/3) voice confirms must SPEAK the resolved target so a fuzzy-salvaged WRONG
argument is audible before the spoken/keyboard yes (fat-thin laptop design follow-up #1)."""
import time
import types

from gabagent.config.models import GabAgentConfig
from gabagent.credits import CreditBalance
from gabagent.credits import credits as C
from gabagent.permissions.voice_approve import _summarize, _summarize_command, _primary_target


def _ctx_img(config=None):
    return types.SimpleNamespace(command_catalog=None, config=config or GabAgentConfig())


def test_generate_image_summary_is_brief_and_names_cost():
    s = _summarize("generate_image", {"prompt": "a red bicycle"}, _ctx_img())
    assert "of a red bicycle" in s and "credits" in s.lower()


def test_generate_image_summary_appends_low_balance_note(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "credits_path", lambda: tmp_path / "c.json")
    C.write_cache(CreditBalance.from_record({"total_available": 30}, fetched_at=time.time()))
    cfg = GabAgentConfig()
    cfg.credits_low_threshold = 100
    s = _summarize("generate_image", {"prompt": "a cat"}, _ctx_img(cfg))
    assert "low on credits" in s and "30" in s


def test_generate_image_summary_no_note_when_guard_off(monkeypatch, tmp_path):
    monkeypatch.setattr(C, "credits_path", lambda: tmp_path / "c.json")
    C.write_cache(CreditBalance.from_record({"total_available": 30}, fetched_at=time.time()))
    s = _summarize("generate_image", {"prompt": "a cat"}, _ctx_img())  # threshold 0 (default)
    assert "low on credits" not in s and s.endswith("a few credits.")


def test_generate_image_summary_truncates_long_prompt():
    ctx = types.SimpleNamespace(command_catalog=None)
    long = "a photorealistic sprawling cyberpunk cityscape at night with neon signage everywhere"
    s = _summarize("generate_image", {"prompt": long}, ctx)
    assert "…" in s and "credits" in s.lower() and len(s) < 120


def test_generate_image_summary_without_prompt_still_names_cost():
    ctx = types.SimpleNamespace(command_catalog=None)
    s = _summarize("generate_image", {}, ctx)
    assert "credits" in s.lower() and s.startswith("Generate an image")


def _ctx_with_command(*, summary="", confirm_template=""):
    cmd = types.SimpleNamespace(summary=summary, confirm_template=confirm_template)
    cat = types.SimpleNamespace(get=lambda cid: cmd)
    return types.SimpleNamespace(command_catalog=cat)


def test_template_path_unchanged_and_names_target():
    """A command WITH a confirm_template already names its target — that path is untouched."""
    ctx = _ctx_with_command(summary="x", confirm_template="Play {title} in Jellyfin?")
    s = _summarize_command({"command_id": "jellyfin.play", "args": {"title": "Heat"}}, ctx)
    assert s == "Play Heat in Jellyfin?"


def test_fallback_appends_resolved_target():
    """No confirm_template → the static summary gets the resolved target appended, so a wrong-salvage
    (e.g. a garbled branch name fuzzy-matched to the wrong real branch) is spoken before the yes."""
    ctx = _ctx_with_command(summary="Switch branch.", confirm_template="")
    s = _summarize_command({"command_id": "git.switch", "args": {"branch": "release-x"}}, ctx)
    assert "release-x" in s and s.startswith("Switch branch")


def test_fallback_no_duplicate_when_summary_already_has_target():
    ctx = _ctx_with_command(summary="Play the release-x playlist.", confirm_template="")
    s = _summarize_command({"command_id": "tidal.play", "args": {"playlist": "release-x"}}, ctx)
    assert s.count("release-x") == 1  # not appended twice


def test_fallback_without_target_is_bare_summary():
    ctx = _ctx_with_command(summary="Run a thing.", confirm_template="")
    assert _summarize_command({"command_id": "x.y", "args": {}}, ctx) == "Run a thing."


def test_primary_target_allowlist_only():
    assert _primary_target({"branch": "release-x"}) == "release-x"
    assert _primary_target({"title": "Heat"}) == "Heat"
    assert _primary_target({"secret": "do-not-speak"}) == ""   # non-allowlisted key never spoken
    assert _primary_target({}) == ""


def test_primary_target_length_capped():
    long = "a-very-long-title-" * 10
    out = _primary_target({"title": long})
    assert out.endswith("…") and len(out) <= 61
