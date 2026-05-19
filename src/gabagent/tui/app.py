from __future__ import annotations
import signal
import asyncio
from gabagent.tui.renderer import console


def setup_signal_handlers(cancel_current_task: asyncio.Event) -> None:
    loop = asyncio.get_event_loop()

    def _sigint_handler():
        cancel_current_task.set()

    loop.add_signal_handler(signal.SIGINT, _sigint_handler)
    loop.add_signal_handler(signal.SIGTERM, lambda: _graceful_shutdown(loop))


def _graceful_shutdown(loop: asyncio.AbstractEventLoop) -> None:
    console.print("\n[info]Shutting down...[/info]", markup=True)
    loop.stop()
