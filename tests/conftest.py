import pytest

from gabagent.commands import usage


@pytest.fixture(autouse=True)
def _isolate_usage_tally(tmp_path_factory, monkeypatch):
    """Keep the per-host command-usage tally out of tests. It's a module-global file under the real
    data dir, so without this a developer's actual usage (recorded by live `run_command`s) leaks into
    hot-set assertions and makes them nondeterministic."""
    p = tmp_path_factory.mktemp("usage") / "command_usage.json"
    monkeypatch.setattr(usage, "_PATH", p)
    yield
