"""`gab --credits` — fetch the live credit balance and print it (also refreshes the cache the
low-balance guard reads). Offline → falls back to the cached balance with a notice."""
from __future__ import annotations


def print_diagnostic(cfg) -> None:
    from gabagent.credits.credits import credits_path, load_cached, refresh
    from gabagent.tui.renderer import console

    bal = None
    if getattr(cfg, "api_key", ""):
        try:
            bal = refresh(cfg.base_url, cfg.api_key)
            console.print(
                f"[green]✓[/green] Live balance from [dim]{cfg.base_url}[/dim] "
                f"→ cached [dim]{credits_path()}[/dim]", markup=True)
        except Exception as e:  # noqa: BLE001
            bal = load_cached()
            where = (f"cached (age {int(bal.age_secs())}s)" if bal else "no cache")
            console.print(
                f"[yellow]○[/yellow] Live fetch failed ([dim]{type(e).__name__}: {e}[/dim]); "
                f"showing {where}.", markup=True)
    else:
        bal = load_cached()
        console.print(
            "[yellow]○[/yellow] No Aria/Gab api_key configured; "
            + ("showing cached balance." if bal else "and no cached balance."), markup=True)

    console.print()
    if bal is None:
        console.print("[dim]No balance to show.[/dim]", markup=True)
        return

    plus = "[green]Plus[/green]" if bal.is_plus else "[dim]free[/dim]"
    console.print(f"[bold]Credit balance:[/bold] [bold cyan]{bal.total_available}[/bold cyan] available  ({plus})", markup=True)
    console.print(
        f"  monthly: [bold]{bal.monthly_remaining}[/bold] of {bal.monthly_total} left"
        + (f" (used {bal.monthly_used})" if bal.monthly_total else "")
        + (f", resets {bal.reset_date[:10]}" if bal.reset_date else ""), markup=True)
    console.print(f"  purchased: [bold]{bal.purchased_available}[/bold]", markup=True)

    thr = int(getattr(cfg, "credits_low_threshold", 0) or 0)
    console.print()
    if thr <= 0:
        console.print("[dim]Low-balance guard: OFF (credits_low_threshold=0).[/dim]", markup=True)
    elif bal.is_low(thr):
        console.print(f"[yellow]⚠ Low-balance guard: ON — below threshold ({bal.total_available} < {thr}). "
                      f"Spend tools will add a heads-up.[/dim]", markup=True)
    else:
        console.print(f"[dim]Low-balance guard: ON, above threshold ({bal.total_available} ≥ {thr}).[/dim]", markup=True)
