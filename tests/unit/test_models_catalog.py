"""Tests for the live model-catalog validation (Aria/Gab /v1/models → router ladder)."""
import json
import time

from gabagent.agent.router import ModelRouter
from gabagent.config.models import GabAgentConfig, Rung
from gabagent.models_catalog import ModelCatalog, ModelInfo, validate_ladder
from gabagent.models_catalog import catalog as C


def _cat(*specs):
    """Build a catalog from (id, function_calling, is_plus_only) triples."""
    models = {}
    for mid, fc, plus in specs:
        models[mid] = ModelInfo.from_record({
            "id": mid,
            "capabilities": {"function_calling": fc},
            "is_plus_only": plus,
            "context_window": 131072,
            "max_output_tokens": 8192,
        })
    return ModelCatalog(models=models, fetched_at=time.time(), source="live")


def _r(model, backend="gab"):
    return Rung(model=model, effort="", backend=backend)


# ── validate_ladder ─────────────────────────────────────────────────────────

def test_empty_catalog_keeps_all_unchecked():
    # The load-bearing fail-safe: no catalog → nothing is dropped and nothing is even checked.
    rungs = [_r("arya"), _r("opus", "claude")]
    kept, verdicts = validate_ladder(rungs, ModelCatalog.empty())
    assert [k.model for k in kept] == ["arya", "opus"]
    assert all(v.keep and not v.checked for v in verdicts)


def test_drops_unknown_gab_model():
    cat = _cat(("arya", True, False))
    kept, verdicts = validate_ladder([_r("arya"), _r("ghost-model")], cat)
    assert [k.model for k in kept] == ["arya"]
    ghost = next(v for v in verdicts if v.model == "ghost-model")
    assert ghost.keep is False and "not in catalog" in ghost.reason


def test_drops_non_tool_capable_gab_model():
    cat = _cat(("arya", True, False), ("embed-x", False, False))
    kept, verdicts = validate_ladder([_r("arya"), _r("embed-x")], cat)
    assert [k.model for k in kept] == ["arya"]
    v = next(x for x in verdicts if x.model == "embed-x")
    assert v.keep is False and "function_calling" in v.reason


def test_plus_only_kept_with_warn():
    # No per-key entitlement signal → keep it (runtime degraded-set handles a key without Plus), warn.
    cat = _cat(("arya", True, False), ("claude-haiku-4-5", True, True))
    kept, verdicts = validate_ladder([_r("arya"), _r("claude-haiku-4-5")], cat)
    assert [k.model for k in kept] == ["arya", "claude-haiku-4-5"]
    v = next(x for x in verdicts if x.model == "claude-haiku-4-5")
    assert v.keep is True and v.warn is True and "is_plus_only" in v.reason


def test_claude_and_local_backends_kept_unchecked():
    # The gab catalog isn't authoritative for anthropic-direct or local rungs — keep them unchecked,
    # even when the same id would be plus-only if it were gab-proxied.
    cat = _cat(("claude-haiku-4-5", True, True))
    kept, verdicts = validate_ladder([_r("claude-haiku-4-5", "claude"), _r("llama3", "local")], cat)
    assert len(kept) == 2
    assert all(v.keep and not v.checked for v in verdicts)


# ── cache round-trip / robustness ───────────────────────────────────────────

def test_cache_roundtrip_and_source(tmp_path):
    p = tmp_path / "cat.json"
    C.write_cache(_cat(("arya", True, False)), p)
    C._memo["mtime"] = None
    loaded = C.load_catalog(p)
    assert loaded and loaded.source == "cache"
    assert loaded.get("arya").function_calling is True


def test_version_mismatch_is_empty(tmp_path):
    p = tmp_path / "cat.json"
    C.write_cache(_cat(("arya", True, False)), p)
    d = json.loads(p.read_text())
    d["version"] = 999
    p.write_text(json.dumps(d))
    C._memo["mtime"] = None
    assert not C.load_catalog(p)


def test_missing_and_corrupt_are_empty(tmp_path):
    C._memo["mtime"] = None
    assert not C.load_catalog(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json")
    C._memo["mtime"] = None
    assert not C.load_catalog(bad)


def test_memo_invalidates_on_rewrite(tmp_path):
    p = tmp_path / "cat.json"
    C.write_cache(_cat(("arya", True, False)), p)
    C._memo["mtime"] = None
    assert C.load_catalog(p).get("arya") is not None
    # rewrite with a different model; mtime changes → memo must not serve the stale catalog
    time.sleep(0.01)
    C.write_cache(_cat(("newmodel", True, False)), p)
    reloaded = C.load_catalog(p)
    assert reloaded.get("newmodel") is not None and reloaded.get("arya") is None


# ── router.assemble integration ─────────────────────────────────────────────

def _assemble_cfg():
    cfg = GabAgentConfig()
    cfg.provider = "claude"
    cfg.api_key = "gab_test"        # include the gab rung
    cfg.claude.api_key = "sk-test"  # include the claude rungs
    cfg.router.cross_backend = True
    return cfg


def test_assemble_drops_bad_gab_rung_keeps_claude(tmp_path, monkeypatch):
    cfg = _assemble_cfg()
    cfg.router.simple_model = "arya-gone"
    C.write_cache(_cat(("something-else", True, False)), tmp_path / "c.json")
    monkeypatch.setattr(C, "catalog_path", lambda: tmp_path / "c.json")
    C._memo["mtime"] = None
    r = ModelRouter.assemble(cfg)
    assert r is not None
    assert "arya-gone" not in [x.model for x in r.ladder]      # dropped
    assert any(x.backend == "claude" for x in r.ladder)         # claude rungs survive


def test_assemble_respects_validate_off(tmp_path, monkeypatch):
    cfg = _assemble_cfg()
    cfg.router.simple_model = "arya-gone"
    cfg.models_catalog_validate = False
    C.write_cache(_cat(("something-else", True, False)), tmp_path / "c.json")
    monkeypatch.setattr(C, "catalog_path", lambda: tmp_path / "c.json")
    C._memo["mtime"] = None
    r = ModelRouter.assemble(cfg)
    assert "arya-gone" in [x.model for x in r.ladder]           # not dropped when validation off


def test_assemble_empty_cache_is_noop(tmp_path, monkeypatch):
    cfg = _assemble_cfg()
    cfg.router.simple_model = "arya"
    monkeypatch.setattr(C, "catalog_path", lambda: tmp_path / "absent.json")  # no cache
    C._memo["mtime"] = None
    r = ModelRouter.assemble(cfg)
    assert "arya" in [x.model for x in r.ladder]                # unchanged from today
