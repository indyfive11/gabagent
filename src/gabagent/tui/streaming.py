from __future__ import annotations
import sys
from rich.console import Console


class StreamingDisplay:
    def __init__(self, console: Console):
        self.console = console
        self._buffer = ""
        self._active = False
        self._model = ""
        self._header_printed = False

    def start(self, model: str = "") -> None:
        self._buffer = ""
        self._active = True
        self._model = model
        self._header_printed = False
        self.console.file.flush()

    def append(self, token: str) -> None:
        self._buffer += token
        if self._active:
            # Print model header lazily on first token so it only appears with real text output
            if not self._header_printed:
                if self._model:
                    self.console.print(f"[dim]{self._model}[/dim]", markup=True)
                    self.console.file.flush()
                self._header_printed = True
            sys.stdout.write(token)
            sys.stdout.flush()

    def stop(self) -> str:
        if self._active:
            if self._header_printed:
                sys.stdout.write("\n")
                sys.stdout.flush()
            self._active = False
        final = self._buffer
        self._buffer = ""
        return final
