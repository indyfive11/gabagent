from __future__ import annotations
from rich.console import Console
from rich.panel import Panel
from rich.text import Text


def render_diagnostic(
    console: Console,
    title: str,
    context: str,
    cause: str,
    recovery: str,
) -> None:
    """Render a structured diagnostic panel for errors."""
    content = Text()
    content.append("⚠ ", style="bold yellow")
    content.append(title, style="bold yellow")
    content.append("\n")
    
    content.append("Context: ", style="dim white")
    content.append(context, style="white")
    content.append("\n")
    
    content.append("Cause: ", style="dim white")
    content.append(cause, style="white")
    content.append("\n")
    
    content.append("Recovery: ", style="dim white")
    content.append(recovery, style="white")
    
    panel = Panel(
        content,
        title="[bold dim]DIAGNOSTIC[/bold dim]",
        border_style="dim yellow",
        padding=(0, 1),
    )
    console.print(panel)
