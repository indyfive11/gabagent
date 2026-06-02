"""Launch desktop apps / open URLs (Tier 2 — scoped, visible, reversible). Uses gtk-launch
for .desktop apps when present, else xdg-open. The {app}/{url} slot is substituted as a single
argv token (no shell), so it can't inject extra commands.
"""
from __future__ import annotations
import shutil
from typing import TYPE_CHECKING

from gabagent.commands.model import Command, Slot, ShellBackend

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


class AppLaunchProvider:
    id = "applaunch"

    async def detect(self, ctx: AgentContext) -> bool:
        return shutil.which("gtk-launch") is not None or shutil.which("xdg-open") is not None

    def commands(self, ctx: AgentContext) -> list[Command]:
        cmds: list[Command] = []
        launcher = ["gtk-launch", "{app}"] if shutil.which("gtk-launch") else ["xdg-open", "{app}"]
        cmds.append(Command(
            id="app.launch", domain="apps", tier=1,
            summary="Launch a desktop application by name",
            confirm_template="Open {app}?",
            backend=ShellBackend(argv=launcher),
            params=[Slot("app", "string", True, description="app or .desktop name, e.g. 'firefox', 'org.kde.konsole'")],
            examples=["open firefox", "launch the calculator"],
        ))
        if shutil.which("xdg-open"):
            cmds.append(Command(
                id="app.open_url", domain="apps", tier=1, featured=True,
                summary="Open a URL in the default browser",
                confirm_template="Open {url} in your browser?",
                backend=ShellBackend(argv=["xdg-open", "{url}"]),
                params=[Slot("url", "string", True)],
                examples=["open github.com", "open my email"],
            ))
        return cmds


PROVIDER = AppLaunchProvider()
