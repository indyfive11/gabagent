from __future__ import annotations
import asyncio
import itertools
from typing import IO

_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_MAGENTA_DIM = "\x1b[2;35m"
_RESET = "\x1b[0m"
_ERASE = "\r\x1b[2K"


class ThinkingIndicator:
    def __init__(self, file: IO[str]) -> None:
        self._file = file
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._active = False

    async def _run(self) -> None:
        for frame in itertools.cycle(_FRAMES):
            if self._stop_event.is_set():
                break
            self._file.write(f"\r{_MAGENTA_DIM}{frame} thinking...{_RESET}")
            self._file.flush()
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break

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
