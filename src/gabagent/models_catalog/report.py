"""`gab --models` — refresh the live catalog and show how it validates the router ladder.

This is the troubleshooting surface: it fetches current truth (and updates the cache the router
reads), then prints the embeddings gate and a per-rung keep/drop/warn verdict so a routing surprise
is diagnosable at a glance. Offline → falls back to the cached catalog with a notice.
"""
from __future__ import annotations


def _candidate_rungs(cfg):
    """The rungs assemble() would consider, WITHOUT runtime gating (local_running / degraded) — the
    tool shows the full static picture the catalog validates, not one session's live subset."""
    from gabagent.api.factory import anthropic_configured
    from gabagent.config.models import Rung

    rungs = []
    if cfg.local_model:
        rungs.append(Rung(model=cfg.local_model, effort="", backend="local"))
    if cfg.api_key:
        rungs.append(Rung(model=cfg.router.simple_model, effort="", backend="gab"))
    if anthropic_configured(cfg):
        rungs.extend(cfg.claude.ladder)
    return rungs


def print_diagnostic(cfg) -> None:
    from gabagent.models_catalog import (
        catalog_path,
        load_catalog,
        refresh_cache,
        validate_ladder,
    )
    from gabagent.tui.renderer import console

    # Refresh live so the tool shows current truth AND updates the cache the router reads.
    if cfg.api_key:
        try:
            cat = refresh_cache(cfg.base_url, cfg.api_key)
            console.print(
                f"[green]✓[/green] Refreshed catalog: [bold]{len(cat)}[/bold] models from "
                f"[dim]{cfg.base_url}[/dim] → [dim]{catalog_path()}[/dim]", markup=True)
        except Exception as e:  # noqa: BLE001
            cat = load_catalog()
            where = (f"cached ({len(cat)} models, age {int(cat.age_secs())}s)"
                     if cat else "no cache")
            console.print(
                f"[yellow]○[/yellow] Live fetch failed ([dim]{type(e).__name__}: {e}[/dim]); "
                f"showing {where}.", markup=True)
    else:
        cat = load_catalog()
        console.print(
            f"[yellow]○[/yellow] No Aria/Gab api_key configured; showing cached catalog "
            f"({len(cat)} models).", markup=True)

    console.print()
    emb = sorted(m.id for m in cat.models.values() if m.capabilities.get("embeddings"))
    console.print(
        "[bold]Embeddings gate:[/bold] "
        + (f"available — {', '.join(emb)}" if emb
           else "[dim]none served on this key (endpoint unbacked → item ① stays blocked)[/dim]"),
        markup=True)

    console.print()
    rungs = _candidate_rungs(cfg)
    kept, verdicts = validate_ladder(rungs, cat)
    console.print("[bold]Router ladder validation:[/bold]", markup=True)
    if not verdicts:
        console.print("  [dim](no rungs configured)[/dim]", markup=True)
    for v in verdicts:
        if not v.checked:
            mark, note = "[dim]— skip[/dim]", f"backend '{v.backend}' not in the gab catalog — kept unchecked"
        elif not v.keep:
            mark, note = "[red]✗ DROP[/red]", v.reason
        elif v.warn:
            mark, note = "[yellow]⚠ keep[/yellow]", v.reason
        else:
            info = cat.get(v.model)
            cw = f", ctx {info.context_window // 1000}k" if info and info.context_window else ""
            mark, note = "[green]✓ keep[/green]", f"tool-capable{cw}"
        console.print(f"  {mark}  [cyan]{v.model}[/cyan] [dim]({v.backend})[/dim] — {note}", markup=True)

    dropped = [v for v in verdicts if v.checked and not v.keep]
    console.print()
    state = "ON" if cfg.models_catalog_validate else "OFF (models_catalog_validate=false)"
    tail = f"; {len(dropped)} dropped" if dropped else ""
    console.print(
        f"[dim]{len(kept)}/{len(verdicts)} rungs kept{tail}. Validation {state}.[/dim]",
        markup=True)
