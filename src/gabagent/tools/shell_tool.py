from __future__ import annotations
import asyncio
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING
from gabagent.api.models import ToolResult
from .base import ToolBase
from .registry import registry

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_SENTINEL_PREFIX = "__GABAGENT_DONE_"
_CMD_TIMEOUT = 120


@dataclass
class ShellState:
    proc: subprocess.Popen
    lock: threading.Lock = field(default_factory=threading.Lock)
    cwd: str = ""
    _output_queue: queue.Queue = field(default_factory=queue.Queue)
    _reader_thread: threading.Thread | None = None

    def __post_init__(self):
        self._start_reader()

    def _start_reader(self):
        def _reader():
            for line in self.proc.stdout:
                self._output_queue.put(line)
        self._reader_thread = threading.Thread(target=_reader, daemon=True)
        self._reader_thread.start()

    def close(self):
        try:
            self.proc.stdin.write("exit\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def _get_or_create_shell(ctx: AgentContext) -> ShellState:
    if ctx.shell_state is None:
        proc = subprocess.Popen(
            ["bash", "--norc", "--noprofile"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env={**os.environ},
        )
        ctx.shell_state = ShellState(proc=proc, cwd=str(ctx.cwd))
    return ctx.shell_state


def _run_in_shell(state: ShellState, command: str, timeout: int = _CMD_TIMEOUT) -> tuple[str, int]:
    sentinel = f"{_SENTINEL_PREFIX}{int(time.time() * 1000)}__"
    wrapped = f"{command}\necho '{sentinel}:'$?\n"

    with state.lock:
        state.proc.stdin.write(wrapped)
        state.proc.stdin.flush()

        output_lines = []
        deadline = time.monotonic() + timeout
        exit_code = 0

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                output_lines.append(f"\n[Timeout after {timeout}s]")
                break
            try:
                line = state._output_queue.get(timeout=min(remaining, 1.0))
                if sentinel in line:
                    try:
                        exit_code = int(line.split(":")[-1].strip())
                    except ValueError:
                        exit_code = 0
                    break
                output_lines.append(line)
            except queue.Empty:
                continue

        # Track cwd
        state.proc.stdin.write(f"echo '__CWD__:'\"$PWD\"\n")
        state.proc.stdin.flush()
        cwd_deadline = time.monotonic() + 5
        while True:
            remaining = cwd_deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                line = state._output_queue.get(timeout=min(remaining, 0.5))
                if line.startswith("__CWD__:"):
                    state.cwd = line[len("__CWD__:"):].strip()
                    break
            except queue.Empty:
                break

        output = "".join(output_lines)
        if len(output) > 30000:
            output = output[:30000] + "\n...[output truncated at 30,000 chars]"
        return output, exit_code


@registry.register
class BashTool(ToolBase):
    name = "bash"
    description = (
        "Execute a shell command. The shell is persistent across calls — "
        "environment variables and directory changes are preserved. "
        "Timeout: 120 seconds."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "timeout": {"type": "integer", "description": "Timeout in seconds (max 120)"},
        },
        "required": ["command"],
    }
    allows_parallel = False

    async def execute(
        self, ctx: AgentContext, command: str, timeout: int = _CMD_TIMEOUT, **kwargs: Any
    ) -> ToolResult:
        from gabagent.plan.mode import is_bash_readonly
        if ctx.plan_mode and not is_bash_readonly(command):
            return ToolResult(
                output="",
                error=f"Blocked in plan mode: mutating command. Read-only commands only.",
            )

        state = _get_or_create_shell(ctx)
        timeout = min(timeout or _CMD_TIMEOUT, _CMD_TIMEOUT)

        loop = asyncio.get_event_loop()
        output, exit_code = await loop.run_in_executor(
            None, lambda: _run_in_shell(state, command, timeout)
        )
        if exit_code != 0:
            return ToolResult(output=output, error=f"Exit code {exit_code}", exit_code=exit_code)
        return ToolResult(output=output or "(no output)", exit_code=0)
