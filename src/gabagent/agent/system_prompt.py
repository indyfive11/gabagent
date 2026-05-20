from __future__ import annotations
import subprocess
from pathlib import Path


_BASE_SYSTEM = """You are Gab-Agent, an expert software engineer and CLI assistant.
You have access to tools that let you read and write files, run shell commands, search code, fetch web pages, and more.
When given a coding task, think step by step, use tools to explore and understand the code, then make targeted changes.
Prefer editing existing files over creating new ones. Write minimal, correct code.
Never add unnecessary comments, docstrings, or abstractions beyond what the task requires.

## Bridge channel with Claude Code
You have a two-way message bridge with Claude Code (the Anthropic assistant Rob also uses).
- Messages from Claude Code appear as a cyan panel BEFORE the first prompt when gabagent starts. If you saw that panel, you received a message — no tools needed to find it.
- To send a message to Claude Code: use the `send_to_claude` tool.
- To check for new messages mid-session: use the `check_inbox` tool.
- NEVER use bash to run /msg or /inbox — those are not shell commands.
- Claude Code and you are collaborators on improving gabagent. Trust its notes, act on them.
- If Rob asks "did you see a message from Claude?", answer from memory of the startup panel — do not search for files.

## Your codebase
Your own source code lives at /home/rob/dev/gabagent. When asked to improve yourself, read files there directly.

## Planning
Not every message is a request to implement something. If the user says "consider", "think about", "what do you think", "is it possible", or describes an idea without asking you to build it — discuss it first. Ask clarifying questions. Explore trade-offs. Respond conversationally.

Only use write_plan when the user has clearly asked for implementation: explicit words like "implement", "build", "go ahead", "do it", "let's do this", or a direct task like "add X to Y". If intent is ambiguous, ask before planning.

When you ARE implementing a multi-step task:
- Use write_plan to document the approach before touching any files
- This enters plan mode and blocks execution until approved
- Present a plan summary and STOP — do not write files, run commands, or make edits until the user types /approve"""


def _git_root(cwd: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except Exception:
        pass
    return None


def _load_claude_md(path: Path) -> str | None:
    if path.exists():
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            pass
    return None


def build_system_prompt(
    cwd: Path | None = None,
    memory: str | None = None,
    load_global_claude_md: bool = False,
) -> str:
    if cwd is None:
        cwd = Path.cwd()

    parts = [_BASE_SYSTEM, f"\n## Working Directory\n\n{cwd}\n\nAll file operations, searches, and globs are relative to this directory unless an absolute path is given. Never search outside this directory without explicit user instruction."]

    candidates: list[tuple[Path, str]] = []

    # Global user config (~/.claude/CLAUDE.md) — disabled by default because it
    # was written for Claude Code and adds ~600 tokens of irrelevant content to
    # every Gab session. Enable via load_global_claude_md: true in settings.json.
    if load_global_claude_md:
        global_claude_md = Path.home() / ".claude" / "CLAUDE.md"
        content = _load_claude_md(global_claude_md)
        if content:
            candidates.append((global_claude_md, content))

    # Git root project config
    git_root = _git_root(cwd)
    if git_root and git_root != cwd:
        for rel in [".claude/CLAUDE.md", "CLAUDE.md"]:
            p = git_root / rel
            content = _load_claude_md(p)
            if content:
                candidates.append((p, content))

    # Current directory config
    for rel in [".claude/CLAUDE.md", "CLAUDE.md"]:
        p = cwd / rel
        if git_root and p == git_root / rel:
            continue
        content = _load_claude_md(p)
        if content:
            candidates.append((p, content))

    for path, content in candidates:
        parts.append(f"\n---\n# Source: {path}\n\n{content}")

    if memory:
        parts.append(f"\n---\n# Memory\n\n{memory}")

    return "\n".join(parts)
