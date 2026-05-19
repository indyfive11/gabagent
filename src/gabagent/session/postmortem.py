from pathlib import Path
from gabagent.config.paths import config_dir
import time

def postmortem_dir(cwd: Path | None = None) -> Path:
    if cwd is None:
        cwd = Path.cwd()
    escaped = str(cwd).replace("/", "-").lstrip("-")
    p = config_dir() / "projects" / escaped / "postmortems"
    p.mkdir(parents=True, exist_ok=True)
    return p

class PostMortemManager:
    def __init__(self, cwd: Path | None = None):
        self._dir = postmortem_dir(cwd)

    def log_crash(self, description: str) -> None:
        ts = time.strftime("%Y%m%d-%H%M%S")
        filename = f"{ts}-crash.md"
        content = f"# Crash-Report\n\nTimestamp: {ts}\n\n## Traceback\n{description}\n"
        (self._dir / filename).write_text(content, encoding="utf-8")

    def list_crashes(self) -> list[Path]:
        return sorted(self._dir.glob("*-crash.md"))

    def check_for_crashes(self) -> None:
        crashes = self.list_crashes()
        if not crashes:
            return
        
        from gabagent.tui.renderer import console
        from rich.panel import Panel
        from rich.prompt import Confirm
        
        console.print(Panel(
            f"[yellow]I detected {len(crashes)} crash reports from previous sessions.[/yellow]\n"
            "Would you like to review them now?",
            title="[bold red]⚠️ Crash Detected[/bold red]",
            border_style="red",
        ))
        
        if Confirm.ask("Review crashes?"):
            for crash in crashes:
                console.print(Panel(
                    crash.read_text(),
                    title=crash.name,
                    border_style="dim"
                ))
            if Confirm.ask("Delete these crash reports?"):
                for crash in crashes:
                    crash.unlink()



    def log(self, title: str, description: str) -> None:
        ts = time.strftime("%Y%m%d-%H%M%S")
        filename = f"{ts}-{title.replace(' ', '_').lower()}.md"
        content = f"# Post-Mortem: {title}\n\nTimestamp: {ts}\n\n## Description\n{description}\n"
        (self._dir / filename).write_text(content, encoding="utf-8")
