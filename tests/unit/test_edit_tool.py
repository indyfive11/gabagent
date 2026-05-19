import pytest
from pathlib import Path
from unittest.mock import MagicMock
from gabagent.tools.file_tools import EditTool, ReadFileTool, WriteFileTool
from gabagent.agent.context import AgentContext


def _ctx(tmp_path):
    ctx = MagicMock(spec=AgentContext)
    ctx.cwd = tmp_path
    ctx.plan_mode = False
    return ctx


@pytest.mark.asyncio
async def test_edit_replaces_unique_string(tmp_path):
    f = tmp_path / "test.py"
    f.write_text("hello world\n")
    ctx = _ctx(tmp_path)
    tool = EditTool()
    result = await tool.execute(ctx, path="test.py", old_string="hello", new_string="goodbye")
    assert result.success
    assert f.read_text() == "goodbye world\n"


@pytest.mark.asyncio
async def test_edit_fails_if_not_found(tmp_path):
    f = tmp_path / "test.py"
    f.write_text("hello world\n")
    ctx = _ctx(tmp_path)
    tool = EditTool()
    result = await tool.execute(ctx, path="test.py", old_string="missing", new_string="x")
    assert not result.success
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_edit_fails_if_not_unique(tmp_path):
    f = tmp_path / "test.py"
    f.write_text("foo\nfoo\n")
    ctx = _ctx(tmp_path)
    tool = EditTool()
    result = await tool.execute(ctx, path="test.py", old_string="foo", new_string="bar")
    assert not result.success
    assert "2 times" in result.error


@pytest.mark.asyncio
async def test_edit_blocked_in_plan_mode(tmp_path):
    f = tmp_path / "test.py"
    f.write_text("hello\n")
    ctx = _ctx(tmp_path)
    ctx.plan_mode = True
    tool = EditTool()
    result = await tool.execute(ctx, path="test.py", old_string="hello", new_string="bye")
    assert not result.success
    assert "plan mode" in result.error.lower()


@pytest.mark.asyncio
async def test_read_with_line_numbers(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("line1\nline2\nline3\n")
    ctx = _ctx(tmp_path)
    tool = ReadFileTool()
    result = await tool.execute(ctx, path="test.txt")
    assert "1\tline1" in result.output
    assert "3\tline3" in result.output


@pytest.mark.asyncio
async def test_write_creates_file(tmp_path):
    ctx = _ctx(tmp_path)
    tool = WriteFileTool()
    result = await tool.execute(ctx, path="new.txt", content="hello\n")
    assert result.success
    assert (tmp_path / "new.txt").read_text() == "hello\n"
