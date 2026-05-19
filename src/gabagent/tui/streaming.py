from __future__ import annotations
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
            if not self._header_printed:
                if self._model:
                    self.console.print(
                        f"[gab.accent]▸[/gab.accent] [dim]{self._model}[/dim]",
                        markup=True,
                    )
                    self.console.file.flush()
                self._header_printed = True
            # markup=False: don't interpret model-returned [bold] etc. as Rich markup
            # soft_wrap=True: pass token to terminal as-is; terminal handles physical wrap
            self.console.print(token, end="", markup=False, soft_wrap=True, highlight=False)
            self.console.file.flush()

    def stop(self) -> str:
        if self._active:
            if self._header_printed:
                self.console.print()
                self.console.file.flush()
            self._active = False
        final = self._buffer
        self._buffer = ""
        return final
