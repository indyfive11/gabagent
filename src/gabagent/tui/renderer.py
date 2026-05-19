from __future__ import annotations
from rich.console import Console
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.panel import Panel
from rich.theme import Theme

GAB_ACCENT = "bold magenta"

THEME = Theme({
    "tool.name":  "bold cyan",
    "tool.arg":   "dim white",
    "tool.result": "green",
    "tool.error": "bold red",
    "info":       "dim cyan",
    "warning":    "bold yellow",
    "error":      "bold red",
    "gab.accent": GAB_ACCENT,
})

console = Console(theme=THEME, highlight=False)
err_console = Console(stderr=True, theme=THEME)


def render_markdown(text: str) -> Markdown:
    return Markdown(text, code_theme="monokai")


def render_response(text: str) -> Markdown:
    return Markdown(text, code_theme="monokai")


def render_code(code: str, language: str = "text", line_numbers: bool = False) -> Syntax:
    return Syntax(code, language, theme="monokai", line_numbers=line_numbers)


