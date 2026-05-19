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

    def log(self, title: str, description: str) -> None:
        ts = time.strftime("%Y%m%d-%H%M%S")
        filename = f"{ts}-{title.replace(' ', '_').lower()}.md"
        content = f"# Post-Mortem: {title}\n\nTimestamp: {ts}\n\n## Description\n{description}\n"
        (self._dir / filename).write_text(content, encoding="utf-8")
