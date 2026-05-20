from __future__ import annotations
import asyncio
import itertools
from typing import IO

_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_RESET = "\x1b[0m"
_ERASE = "\r\x1b[2K"

_STATE_COLORS = {
    "THINKING": "\x1b[38;5;63m",      # muted indigo
    "TOOL_EXECUTING": "\x1b[36m",     # cyan
    "ERROR": "\x1b[91m",               # bright red
}


class ThinkingIndicator:
    def __init__(self, file: IO[str]) -> None:
        self._file = file
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._active = False
        self._state = "THINKING"

    async def _run(self) -> None:
        for frame in itertools.cycle(_FRAMES):
            if self._stop_event.is_set():
                break
            color = _STATE_COLORS.get(self._state, _STATE_COLORS["THINKING"])
            label = "thinking..." if self._state == "THINKING" else self._state.lower().replace("_", " ")
            self._file.write(f"\r{color}{frame} {label}{_RESET}")
            self._file.flush()
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break

    def set_state(self, state: str) -> None:
        """Set the visual state: THINKING, TOOL_EXECUTING, ERROR"""
        self._state = state

    def start(self) -> None:
        if self._active:
            return
        self._stop_event.clear()
        self._active = True
        self._task = asyncio.create_task(self._run())

    def stop(self) -> None:
        if not self._active:
            return
        self._stop_event.set()
        self._active = False
        self._file.write(_ERASE)
        self._file.flush()
        self._state = "THINKING"
