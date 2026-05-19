from __future__ import annotations
from typing import TYPE_CHECKING
from gabagent.api.models import ChatMessage
from gabagent.tui.renderer import console

if TYPE_CHECKING:
    from gabagent.api.client import GabAIClient
    from gabagent.config.models import GabAgentConfig

_ROUTING_PROMPT = """\
You are a model router. Classify the complexity of the user's request.
Return ONLY one of these two tags — no explanation, no other text:
[SIMPLE] — file reading, searching, git commands, simple questions, summarization, small edits
[COMPLEX] — writing new classes, multi-file refactoring, complex debugging, architecture changes

User request: {prompt}"""


class ModelRouter:
    def __init__(self, config: GabAgentConfig) -> None:
        self.enabled = config.router.enabled
        self.classifier_enabled = config.router.classifier_enabled
        self.simple_model = config.router.simple_model
        self.complex_model = config.router.complex_model

    async def classify_intent(self, prompt: str, client: GabAIClient) -> str:
        if not self.classifier_enabled:
            return self.simple_model
        try:
            messages = [ChatMessage(role="user", content=_ROUTING_PROMPT.format(prompt=prompt))]
            tag = await client.complete_simple(messages, model=self.simple_model)
            model = self.simple_model if "[SIMPLE]" in tag else self.complex_model
            console.print(f"[dim]→ routing to {model}[/dim]", markup=True)
            return model
        except Exception:
            return self.simple_model

    def check_tool_complexity(self, tool_name: str, args: dict) -> str | None:
        if tool_name in ("write_file", "edit"):
            content = str(args.get("content", args.get("new_string", "")))
            if len(content) > 2000:
                return self.complex_model
        return None

    def check_reactive(
        self, tool_name: str, exit_code: int | None, active_model: str | None
    ) -> str | None:
        current = active_model or self.simple_model
        if tool_name == "bash" and exit_code is not None and exit_code != 0 and current == self.simple_model:
            return self.complex_model
        return None
