from __future__ import annotations
import json
from rich.console import Console
from rich.text import Text


class ToolCallDisplay:
    def __init__(self, console: Console):
        self.console = console

    def show_start(self, name: str, args_json: str) -> None:
        budget = max(20, self.console.width - 35)
        per_arg = min(80, budget // 3)
        try:
            args = json.loads(args_json) if args_json else {}
            args_summary = ", ".join(
                f"{k}={repr(v)[:per_arg]}" for k, v in list(args.items())[:3]
            )
        except Exception:
            args_summary = args_json[:budget] if args_json else ""
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
        line.append(f"{name}: ", style="dim")
        
        # Truncate and sanitize result_text for preview
        if result_text:
            budget = max(60, self.console.width - 20)
            preview = result_text.replace("\n", " ↵ ")
            if len(preview) > budget:
                preview = preview[:budget - 3] + "..."
            line.append(preview, style="dim white")
        else:
            line.append("(empty)", style="dim white")
            
        if extra:
            line.append(" " + extra, style="dim")
            
        self.console.print(line)
