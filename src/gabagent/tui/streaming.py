from __future__ import annotations
from rich.console import Console
from gabagent.tui.thinking import ThinkingIndicator


class StreamingDisplay:
    def __init__(self, console: Console, thinking: ThinkingIndicator | None = None):
        self.console = console
        self._thinking = thinking
        self._buffer = ""
        self._word_buf = ""
        self._col = 0
        self._active = False
        self._model = ""
        self._header_printed = False

    def _width(self) -> int:
        return max(40, self.console.width - 2)

    def _flush_word(self) -> None:
        if not self._word_buf:
            return
        if self._col + len(self._word_buf) > self._width() and self._col > 0:
            self.console.file.write("\n")
            self._col = 0
        self.console.file.write(self._word_buf)
        self._col += len(self._word_buf)
        self._word_buf = ""

    def start(self, model: str = "") -> None:
        self._buffer = ""
        self._word_buf = ""
        self._col = 0
        self._active = True
        self._model = model
        self._header_printed = False
        self.console.file.flush()

    def append(self, token: str) -> None:
        self._buffer += token
        if not self._active:
            return
        if not self._header_printed:
            if self._thinking:
                self._thinking.stop()
            if self._model:
                self.console.print(
                    f"[gab.accent]▸[/gab.accent] [dim]{self._model}[/dim]",
                    markup=True,
                )
                self.console.file.flush()
            self._header_printed = True
            self._col = 0

        for char in token:
            if char == "\n":
                self._flush_word()
                self.console.file.write("\n")
                self._col = 0
            elif char == " ":
                self._flush_word()
                if self._col > 0:
                    self.console.file.write(" ")
                    self._col += 1
            else:
                self._word_buf += char

        self.console.file.flush()

    def stop(self) -> str:
        if self._thinking:
            self._thinking.stop()
        if self._active:
            self._flush_word()
            if self._header_printed:
                self.console.file.write("\n")
                self.console.file.flush()
            self._active = False
        final = self._buffer
        self._buffer = ""
        return final
