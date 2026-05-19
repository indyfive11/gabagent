import pytest
from gabagent.permissions.engine import PermissionEngine, Decision
from gabagent.config.models import GabAgentConfig, PermissionsConfig


def _cfg(allow=None, deny=None, mode="default"):
    return GabAgentConfig(
        api_key="test",
        permissions=PermissionsConfig(allow=allow or [], deny=deny or [], mode=mode),
    )


def test_deny_list_blocks():
    engine = PermissionEngine(_cfg(deny=["write_file"]))
    assert engine.check("write_file", {}) == Decision.DENY


def test_allow_list_passes():
    engine = PermissionEngine(_cfg(allow=["bash"]))
    assert engine.check("bash", {"command": "ls"}) == Decision.ALLOW


def test_default_prompts_unknown():
    engine = PermissionEngine(_cfg())
    assert engine.check("bash", {"command": "ls"}) == Decision.PROMPT


def test_bypass_mode_allows_everything():
    engine = PermissionEngine(_cfg(mode="bypass"))
    assert engine.check("write_file", {"path": "/etc/passwd"}) == Decision.ALLOW


def test_dangerous_rm_always_prompts():
    engine = PermissionEngine(_cfg(allow=["bash(*)"]))
    assert engine.check("bash", {"command": "rm -rf /"}) == Decision.PROMPT


def test_auto_approve_mode():
    engine = PermissionEngine(_cfg(mode="auto_approve"))
    assert engine.check("bash", {"command": "ls"}) == Decision.ALLOW
