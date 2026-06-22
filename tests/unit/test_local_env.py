import types

from gabagent.config.models import GabAgentConfig
from gabagent.config.setup import detect_gpu_env
from gabagent.local.ollama import _local_subprocess_env


def _ctx(**cfg_kw):
    return types.SimpleNamespace(config=GabAgentConfig(api_key="t", **cfg_kw))


# --- local_env overlay on the spawned `ollama serve` env --------------------

def test_local_env_default_injects_nothing(monkeypatch):
    # Universal-safe default: no local_env => no GPU override leaks in. The former hard-coded
    # HSA_OVERRIDE_GFX_VERSION must NOT appear for a generic install.
    monkeypatch.delenv("HSA_OVERRIDE_GFX_VERSION", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")
    env = _local_subprocess_env(_ctx())  # local_env == {}
    assert "HSA_OVERRIDE_GFX_VERSION" not in env
    assert env["PATH"] == "/usr/bin"  # inherits the parent environment


def test_local_env_overlays_config(monkeypatch):
    monkeypatch.delenv("HSA_OVERRIDE_GFX_VERSION", raising=False)
    ctx = _ctx(local_env={"HSA_OVERRIDE_GFX_VERSION": "11.0.0", "OLLAMA_NUM_PARALLEL": "1"})
    env = _local_subprocess_env(ctx)
    assert env["HSA_OVERRIDE_GFX_VERSION"] == "11.0.0"
    assert env["OLLAMA_NUM_PARALLEL"] == "1"


def test_local_env_overrides_inherited(monkeypatch):
    monkeypatch.setenv("HSA_OVERRIDE_GFX_VERSION", "9.0.0")
    ctx = _ctx(local_env={"HSA_OVERRIDE_GFX_VERSION": "11.0.0"})
    assert _local_subprocess_env(ctx)["HSA_OVERRIDE_GFX_VERSION"] == "11.0.0"  # config wins


# --- detect_gpu_env: opt-in, rocminfo-based suggestion ----------------------

def test_detect_gpu_env_no_rocminfo_returns_empty(monkeypatch):
    # No AMD ROCm tool on PATH (e.g. NVIDIA or no GPU) => nothing suggested, universal-safe.
    monkeypatch.setattr("shutil.which", lambda _name: None)
    assert detect_gpu_env() == {}


def test_detect_gpu_env_maps_gfx_to_family_base(monkeypatch):
    cases = {
        "Name:                    gfx1100": "11.0.0",  # RDNA3 dGPU
        "  Name: gfx1103 ":                "11.0.0",   # Phoenix iGPU -> family base
        "gfx1030\n":                       "10.3.0",   # RDNA2
        "gfx900":                          "9.0.0",    # Vega (3-digit, zero-padded)
    }
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/rocminfo")
    for stdout, expected in cases.items():
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, _s=stdout, **k: types.SimpleNamespace(stdout=_s),
        )
        assert detect_gpu_env() == {"HSA_OVERRIDE_GFX_VERSION": expected}


def test_detect_gpu_env_swallows_rocminfo_failure(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/rocminfo")

    def _boom(*a, **k):
        raise OSError("rocminfo crashed")

    monkeypatch.setattr("subprocess.run", _boom)
    assert detect_gpu_env() == {}  # never raises
