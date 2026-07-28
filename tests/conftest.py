import os

import pytest

from gabagent.commands import usage


@pytest.fixture(autouse=True)
def _scrub_gabai_env(monkeypatch):
    """Clear any GABAI_* env var for the duration of each test.

    Config precedence is init/CLI > env > settings.json > defaults, so a developer shell that exports
    GABAI_* (the live brain host does — GABAI_API_KEY, GABAI_VOICE_*, …) would otherwise leak into any
    test that builds config via load_config()/get_config() and make default/file assertions nondeterministic
    on that box vs clean CI. A test that wants a specific GABAI_* sets it explicitly with monkeypatch.setenv."""
    for key in [k for k in os.environ if k.startswith("GABAI_")]:
        monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_usage_tally(tmp_path_factory, monkeypatch):
    """Keep the per-host command-usage tally out of tests. It's a module-global file under the real
    data dir, so without this a developer's actual usage (recorded by live `run_command`s) leaks into
    hot-set assertions and makes them nondeterministic."""
    p = tmp_path_factory.mktemp("usage") / "command_usage.json"
    monkeypatch.setattr(usage, "_PATH", p)
    yield
