from __future__ import annotations
import asyncio
import json
import os
import subprocess
from fnmatch import fnmatch
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from gabagent.config.models import GabAgentConfig


class HookRunner:
    def __init__(self, config: GabAgentConfig):
        self._config = config

    def _match_hooks(self, hook_list, tool_name: str):
        return [h for h in hook_list if fnmatch(tool_name, h.matcher or "*")]

    async def _run_hook(self, command: str, env_extra: dict | None = None) -> tuple[str, int]:
        env = {**os.environ}
        if env_extra:
            env.update({k: str(v) for k, v in env_extra.items()})
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            return stdout.decode(errors="replace"), proc.returncode or 0
        except asyncio.TimeoutError:
            return "", 1
        except Exception as e:
            return "", 1

    async def run_pre_tool(self, tool_name: str, args: dict) -> tuple[bool, str]:
        hooks = self._match_hooks(self._config.hooks.PreToolUse, tool_name)
        for hook in hooks:
            env = {
                "GABAGENT_TOOL_NAME": tool_name,
                "GABAGENT_TOOL_ARGS": json.dumps(args),
            }
            stdout, _ = await self._run_hook(hook.command, env)
            if stdout.strip():
                try:
                    data = json.loads(stdout.strip())
                    if data.get("action") == "block":
                        return True, data.get("reason", "Blocked by hook")
                except Exception:
                    pass
        return False, ""

    async def run_post_tool(self, tool_name: str, args: dict, result: Any) -> None:
        hooks = self._match_hooks(self._config.hooks.PostToolUse, tool_name)
        for hook in hooks:
            env = {
                "GABAGENT_TOOL_NAME": tool_name,
                "GABAGENT_TOOL_ARGS": json.dumps(args),
                "GABAGENT_TOOL_RESULT": json.dumps(result.to_content() if hasattr(result, "to_content") else str(result)),
            }
            await self._run_hook(hook.command, env)

    async def run_user_prompt_submit(self, prompt: str) -> str:
        hooks = self._config.hooks.UserPromptSubmit
        outputs = []
        for hook in hooks:
            env = {"GABAGENT_PROMPT": prompt}
            stdout, _ = await self._run_hook(hook.command, env)
            if stdout.strip():
                outputs.append(stdout.strip())
        return "\n".join(outputs)

    async def run_stop(self, final_response: str) -> None:
        for hook in self._config.hooks.Stop:
            env = {"GABAGENT_RESPONSE": final_response}
            await self._run_hook(hook.command, env)

    async def run_session_start(self) -> None:
        for hook in self._config.hooks.SessionStart:
            await self._run_hook(hook.command)
