import pytest
from pathlib import Path
from gabagent.agent.system_prompt import build_system_prompt


def test_base_prompt_included(tmp_path):
    sp = build_system_prompt(cwd=tmp_path)
    assert "Gab-Agent" in sp
    assert "expert software engineer" in sp.lower()


def test_claude_md_injected(tmp_path):
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text("# Project Rules\nAlways use type hints.")
    sp = build_system_prompt(cwd=tmp_path)
    assert "Always use type hints" in sp


def test_dot_claude_md_injected(tmp_path):
    dot_claude = tmp_path / ".claude"
    dot_claude.mkdir()
    (dot_claude / "CLAUDE.md").write_text("# Dot Claude\nDo not use globals.")
    sp = build_system_prompt(cwd=tmp_path)
    assert "Do not use globals" in sp


def test_memory_injected(tmp_path):
    sp = build_system_prompt(cwd=tmp_path, memory="Remember: use snake_case.")
    assert "Remember: use snake_case" in sp
    assert "# Memory" in sp


def test_no_claude_md_still_works(tmp_path):
    sp = build_system_prompt(cwd=tmp_path)
    assert sp  # not empty
