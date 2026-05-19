from __future__ import annotations
import json
import time
import uuid
from pathlib import Path
from gabagent.api.models import ChatMessage, ToolCallSpec


class SessionFile:
    def __init__(self, path: Path):
        self.path = path
        self._messages: list[ChatMessage] = []
        if path.exists():
            self._load()

    def _load(self) -> None:
        self._messages = []
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            role = obj.get("role", "user")
            content = obj.get("content")
            tool_calls_raw = obj.get("tool_calls")
            tool_calls = None
            if tool_calls_raw:
                tool_calls = [
                    ToolCallSpec(
                        id=tc["id"],
                        name=tc["function"]["name"],
                        arguments=tc["function"]["arguments"],
                    )
                    for tc in tool_calls_raw
                ]
            msg = ChatMessage(
                role=role,
                content=content,
                tool_calls=tool_calls,
                tool_call_id=obj.get("tool_call_id"),
                name=obj.get("name"),
            )
            self._messages.append(msg)

    def _append_line(self, obj: dict) -> None:
        obj["ts"] = time.time()
        with open(self.path, "a") as f:
            f.write(json.dumps(obj) + "\n")

    def append_message(self, msg: ChatMessage) -> None:
        self._messages.append(msg)
        obj = msg.to_dict()
        self._append_line(obj)

    def messages(self) -> list[ChatMessage]:
        return list(self._messages)

    def replace_all(self, messages: list[ChatMessage]) -> None:
        self._messages = list(messages)
        self.path.write_text("")
        for msg in messages:
            obj = msg.to_dict()
            obj["ts"] = time.time()
            with open(self.path, "a") as f:
                f.write(json.dumps(obj) + "\n")
