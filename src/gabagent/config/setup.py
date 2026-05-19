from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gabagent.config.models import GabAgentConfig


async def run_first_time_setup(cfg: GabAgentConfig) -> GabAgentConfig:
    import typer
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    from prompt_toolkit import PromptSession

    console = Console()

    console.clear()
    console.print(Panel(
        Text.assemble(
            ("Gab-Agent\n", "bold white"),
            ("AI coding assistant powered by Gab AI", "dim"),
        ),
        border_style="cyan",
        padding=(1, 4),
    ))

    console.print()
    console.print("[bold]No API key found.[/bold] To use Gabagent you need a Gab AI API key.\n", markup=True)
    console.print("  Get yours at: [cyan]https://gab.ai/settings[/cyan]  (Settings → API Settings)", markup=True)
    console.print("  Requires a [bold]Gab AI Plus[/bold] subscription ($16.67/month).\n", markup=True)

    session = PromptSession()

    # API key prompt
    while True:
        try:
            api_key = await session.prompt_async("Enter your Gab AI API key: ")
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Setup cancelled.[/dim]", markup=True)
            raise typer.Exit(0)

        api_key = api_key.strip()
        if not api_key:
            console.print("[yellow]API key cannot be empty. Try again.[/yellow]\n", markup=True)
            continue
        if len(api_key) < 10:
            console.print("[yellow]Warning: that key looks short — double-check it's correct.[/yellow]", markup=True)
        break

    # Model prompt
    console.print(
        "\n[dim]Default model: [bold]arya[/bold] (free/unlimited). "
        "Press Enter to keep it, or type a model name (e.g. claude-sonnet-4.5).[/dim]\n",
        markup=True,
    )
    try:
        model_input = await session.prompt_async(f"Model name (default: {cfg.model}): ")
        model = model_input.strip() or cfg.model
    except (EOFError, KeyboardInterrupt):
        model = cfg.model

    # Save config
    from gabagent.config.loader import save_config
    from gabagent.config.paths import settings_file

    cfg.api_key = api_key
    cfg.model = model
    save_config(cfg)

    sf = settings_file()
    console.print(f"\n[green]✓[/green] API key saved to [dim]{sf}[/dim]\n", markup=True)
    console.print("[dim]Starting Gabagent...[/dim]\n", markup=True)

    return cfg
