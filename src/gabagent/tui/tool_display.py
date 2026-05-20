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
            parts = []
            for k, v in list(args.items())[:2]:
                vs = str(v).replace("\n", "⏎")
                vt = (vs[:48] + "…") if len(vs) > 48 else vs
                parts.append(f"{k}={repr(vt)}")
            args_summary = ", ".join(parts)
        except Exception:
            raw = (args_json or "")[:60]
            args_summary = (raw[:57] + "…") if len(raw) > 57 else raw
        line = Text()
        line.append("  ⚙ ", style="dim")
        line.append(name, style="tool.name")
        line.append("(", style="dim")
        line.append(args_summary, style="dim white")
        line.append(")", style="dim")
        self.console.print(line)

    def show_result(self, name: str, result_text: str, is_error: bool = False, extra: str = "") -> None:
        icon = "✗" if is_error else "✓"
        style = "tool.error" if is_error else "tool.result"

        line = Text()
        line.append(f"  {icon} ", style=style)
        line.append(f"{name}:", style="dim")

        if result_text and result_text.strip():
            first = next((l.strip() for l in result_text.splitlines() if l.strip()), "")
            preview = (first[:70] + "…") if len(first) > 70 else first
            line.append(f" {preview}", style="dim white")
        else:
            line.append(" (no output)", style="dim white")

        if extra:
            line.append(f" {extra.strip()}", style="dim")

        self.console.print(line)
