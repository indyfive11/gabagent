from __future__ import annotations
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from gabagent.config.paths import history_file

# Enter = submit, Meta/Alt+Enter = insert newline for multi-line input
_KB = KeyBindings()

@_KB.add('enter')
def _submit(event):
    event.current_buffer.validate_and_handle()

@_KB.add('escape', 'enter')   # Alt+Enter / Meta+Enter
def _newline(event):
    event.current_buffer.newline()


class InputHandler:
    def __init__(self, vim_mode: bool = False):
        self._session: PromptSession = PromptSession(
            history=FileHistory(str(history_file())),
            auto_suggest=AutoSuggestFromHistory(),
            vi_mode=vim_mode,
            multiline=True,
            complete_while_typing=True,
            enable_open_in_editor=True,
        )

    async def prompt(self, badge: str = "") -> str | None:
        from gabagent.tui.renderer import console
        console.print("", markup=False)
        if badge:
            pt: str | FormattedText = FormattedText([
                ("bold fg:ansimagenta", "◆ "),
                ("bold fg:ansibrightcyan", "["),
                ("bold fg:ansibrightcyan", badge),
                ("bold fg:ansibrightcyan", "]"),
                ("bold", " ▶ "),
            ])
        else:
            pt: str | FormattedText = FormattedText([
                ("bold fg:ansimagenta", "◆ "),
                ("bold", "▶ "),
            ])
        try:
            return await self._session.prompt_async(pt, key_bindings=_KB)
        except (EOFError, KeyboardInterrupt):
            return None
