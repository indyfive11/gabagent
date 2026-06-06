from gabagent.api.factory import build_client, LLMClient
from gabagent.api.client import GabAIClient
from gabagent.api.claudette import ClaudetteClient
from gabagent.config.models import GabAgentConfig
from gabagent.api.rate_limit import UsageTracker


def _rl():
    return UsageTracker(simple_model="arya")


def test_factory_gab_default():
    cfg = GabAgentConfig(api_key="k")
    cl = build_client(cfg, _rl())
    assert isinstance(cl, GabAIClient)
    assert cl.model == "arya"


def test_factory_claude():
    cfg = GabAgentConfig()
    cfg.provider = "claude"
    cfg.claude.api_key = "sk-test"
    cl = build_client(cfg, _rl())
    assert isinstance(cl, ClaudetteClient)
    # bottom rung is the base model
    assert cl.model == "claude-haiku-4-5"
    assert cl.effort == ""


def test_factory_model_override():
    cfg = GabAgentConfig()
    cfg.provider = "claude"
    cfg.claude.api_key = "sk-test"
    cl = build_client(cfg, _rl(), model="claude-opus-4-8")
    assert cl.model == "claude-opus-4-8"


def test_clients_satisfy_protocol():
    assert isinstance(build_client(GabAgentConfig(api_key="k"), _rl()), LLMClient)
    cfg = GabAgentConfig(); cfg.provider = "claude"; cfg.claude.api_key = "x"
    assert isinstance(build_client(cfg, _rl()), LLMClient)
