from __future__ import annotations
import json
from rich.console import Console
from rich.text import Text


class ToolCallDisplay:
    def __init__(self, console: Console):
        self.console = console

    def show_start(self, name: str, args_json: str) -> None:
        try:
            args = json.loads(args_json) if args_json else {}
            args_summary = ", ".join(
                f"{k}={repr(v)[:60]}" for k, v in list(args.items())[:3]
            )
        except Exception:
            args_summary = args_json[:80] if args_json else ""
        line = Text()
        line.append("  ⚙ ", style="dim")
        line.append(name, style="bold cyan")
        line.append("(", style="dim")
        line.append(args_summary, style="dim white")
        line.append(")", style="dim")
        self.console.print(line)

    def show_result(self, name: str, result_text: str, is_error: bool = False, extra: str = "") -> None:
        icon = "✗" if is_error else "✓"
        style = "bold red" if is_error else "green"
        preview = result_text[:140].replace("\n", " ↵ ") if result_text else "(empty)"
        line = Text()
        line.append(f"  {icon} ", style=style)
        line.append(f"{name}: ", style="dim")
        line.append(preview, style="dim white")
        if extra:
            line.append(extra, style="dim")
        self.console.print(line)
