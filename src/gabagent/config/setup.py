from __future__ import annotations
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gabagent.config.models import GabAgentConfig


def backend_configured(cfg: GabAgentConfig) -> bool:
    """Is the selected primary backend ready to run (no first-time setup needed)?"""
    if cfg.provider == "claude":
        return bool(cfg.claude.api_key or os.environ.get("ANTHROPIC_API_KEY"))
    if cfg.local_model:
        return True  # local Ollama path uses a placeholder key, not a gab.ai key
    return bool(cfg.api_key)


async def run_first_time_setup(cfg: GabAgentConfig) -> GabAgentConfig:
    """Claude-Code-style backend picker: choose Gab AI / Claude / Local, then configure it."""
    import typer
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    from prompt_toolkit import PromptSession

    console = Console()
    session = PromptSession()

    console.clear()
    console.print(Panel(
        Text.assemble(
            ("Gab-Agent\n", "bold white"),
            ("Pluggable AI coding assistant", "dim"),
        ),
        border_style="cyan",
        padding=(1, 4),
    ))

    console.print()
    console.print("[bold]Choose a backend (the \"brain\"):[/bold]\n", markup=True)
    console.print("  [cyan]1[/cyan]  Gab AI       — gab.ai models (default: arya, free/unlimited)", markup=True)
    console.print("  [cyan]2[/cyan]  Claude       — Anthropic API (model/effort escalation ladder)", markup=True)
    console.print("  [cyan]3[/cyan]  Local        — Ollama on this machine", markup=True)
    console.print()

    try:
        choice = (await session.prompt_async("Backend [1/2/3] (default: 1): ")).strip() or "1"
    except (EOFError, KeyboardInterrupt):
        console.print("\n[dim]Setup cancelled.[/dim]", markup=True)
        raise typer.Exit(0)

    if choice == "2":
        cfg = await _setup_claude(cfg, console, session)
    elif choice == "3":
        cfg = await _setup_local(cfg, console, session)
    else:
        cfg = await _setup_gab(cfg, console, session)

    from gabagent.config.loader import save_config
    from gabagent.config.paths import settings_file
    save_config(cfg)
    console.print(f"\n[green]✓[/green] Saved to [dim]{settings_file()}[/dim]\n", markup=True)
    console.print("[dim]Starting Gabagent...[/dim]\n", markup=True)
    return cfg


async def _prompt(session, prompt_text: str, default: str = "") -> str:
    import typer
    try:
        val = (await session.prompt_async(prompt_text)).strip()
    except (EOFError, KeyboardInterrupt):
        if default:
            return default
        raise typer.Exit(0)
    return val or default


async def _setup_gab(cfg: GabAgentConfig, console, session) -> GabAgentConfig:
    console.print(
        "\n[bold]Gab AI[/bold] — get a key at [cyan]https://gab.ai/settings[/cyan] "
        "(Settings → API Settings). Requires Gab AI Plus.\n",
        markup=True,
    )
    while True:
        api_key = await _prompt(session, "Enter your Gab AI API key: ")
        if not api_key:
            console.print("[yellow]API key cannot be empty. Try again.[/yellow]\n", markup=True)
            continue
        if len(api_key) < 10:
            console.print("[yellow]Warning: that key looks short — double-check it.[/yellow]", markup=True)
        break

    console.print(
        "\n[dim]Default model: [bold]arya[/bold] (free/unlimited). Press Enter to keep it, "
        "or type another gab.ai model name.[/dim]\n",
        markup=True,
    )
    model = await _prompt(session, f"Model name (default: {cfg.model}): ", default=cfg.model)

    cfg.provider = "gab"
    cfg.api_key = api_key
    cfg.model = model
    return cfg


async def _setup_claude(cfg: GabAgentConfig, console, session) -> GabAgentConfig:
    env_key = os.environ.get("ANTHROPIC_API_KEY", "")
    console.print(
        "\n[bold]Claude (Anthropic)[/bold] — get a key at "
        "[cyan]https://console.anthropic.com/settings/keys[/cyan].\n",
        markup=True,
    )
    if env_key:
        console.print(
            "[dim]ANTHROPIC_API_KEY is set in your environment — press Enter to use it.[/dim]\n",
            markup=True,
        )
    api_key = await _prompt(session, "Enter your Anthropic API key: ", default=env_key)
    if not api_key and not env_key:
        console.print("[yellow]No key entered; the SDK will read ANTHROPIC_API_KEY at runtime.[/yellow]", markup=True)

    bottom = cfg.claude.ladder[0].model
    console.print(
        f"\n[dim]The escalation ladder (bottom rung [bold]{bottom}[/bold]) routes each turn to the "
        "cheapest capable (model, effort). Press Enter to keep the default ladder.[/dim]\n",
        markup=True,
    )

    cfg.provider = "claude"
    cfg.claude.api_key = api_key
    cfg.model = bottom
    return cfg


def detect_gpu_env() -> dict[str, str]:
    """One-shot, opt-in GPU-arch detection for the local-model server (called only from the Local
    backend setup). On an AMD ROCm box, `rocminfo` (the authoritative OS source) reports the gfx
    target; HSA_OVERRIDE_GFX_VERSION must be a ROCm-supported version string matching the family
    base ("11.0.0" for the gfx1100 family, "10.3.0" for gfx1030). We derive the conventional family
    base from the detected target and return it as a SUGGESTION. Returns {} when no AMD GPU /
    rocminfo is present (NVIDIA needs no override; empty = the universal-safe default). Never raises,
    never installs anything — pure read."""
    import shutil
    import subprocess
    import re

    if not shutil.which("rocminfo"):
        return {}
    try:
        out = subprocess.run(["rocminfo"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return {}
    m = re.search(r"gfx(\d{3,4})\b", out)
    if not m:
        return {}
    # HSA_OVERRIDE "X.Y.Z" mirrors gfxXYZ; the trailing stepping digit folds to 0 (the family base
    # ROCm ships): gfx1100/gfx1103 -> 11.0.0, gfx1030 -> 10.3.0, gfx900 -> 9.0.0.
    g = m.group(1).zfill(4)
    return {"HSA_OVERRIDE_GFX_VERSION": f"{int(g[:2])}.{int(g[2])}.0"}


async def _setup_local(cfg: GabAgentConfig, console, session) -> GabAgentConfig:
    console.print(
        "\n[bold]Local (Ollama)[/bold] — runs a model on this machine via Ollama "
        f"([dim]{cfg.local_base_url}[/dim]). Opt-in: nothing is installed or downloaded for you. "
        "Ollama must already be installed, and the model is pulled on first use.\n",
        markup=True,
    )
    default_model = cfg.local_model or "qwen2.5-coder"
    model = await _prompt(session, f"Local model name (default: {default_model}): ", default=default_model)

    cfg.provider = "gab"  # primary stays gab-shaped; the local client is OpenAI-compatible
    cfg.local_model = model

    # Detect-and-write the GPU env override (SOP: authoritative source, written once, user-editable).
    # Only an AMD ROCm card needs one; we suggest the family base and let the user confirm/skip/edit.
    if not cfg.local_env:
        suggested = detect_gpu_env().get("HSA_OVERRIDE_GFX_VERSION", "")
        if suggested:
            console.print(
                f"\n[dim]Detected an AMD ROCm GPU. Suggested "
                f"[bold]HSA_OVERRIDE_GFX_VERSION={suggested}[/bold]. Press Enter to use it, type "
                "another value, or 'none' to inject nothing.[/dim]\n",
                markup=True,
            )
            val = await _prompt(
                session, f"HSA_OVERRIDE_GFX_VERSION (default: {suggested}): ", default=suggested
            )
            if val and val.lower() != "none":
                cfg.local_env = {"HSA_OVERRIDE_GFX_VERSION": val}
    return cfg
