from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ChatMessage:
    role: str
    content: str | None = None
    tool_calls: list[ToolCallSpec] | None = None
    tool_call_id: str | None = None
    name: str | None = None

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            d["content"] = self.content
        if self.tool_calls:
            d["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d


@dataclass
class ToolCallSpec:
    id: str
    name: str
    arguments: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": self.arguments,
            },
        }


@dataclass
class ToolResult:
    output: str
    error: str | None = None
    exit_code: int = 0

    @property
    def success(self) -> bool:
        return self.error is None and self.exit_code == 0

    def to_content(self) -> str:
        if self.error:
            return f"Error: {self.error}"
        return self.output
