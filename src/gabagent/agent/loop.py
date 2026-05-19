from __future__ import annotations
import asyncio
import json
from typing import TYPE_CHECKING
from gabagent.api.models import ChatMessage, ToolCallSpec, ToolResult
from gabagent.tui.renderer import console
from gabagent.tui.streaming import StreamingDisplay
from gabagent.tui.tool_display import ToolCallDisplay

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext
    from gabagent.agent.router import ModelRouter

CONTEXT_WARN_RATIO = 0.7
CONTEXT_COMPACT_RATIO = 0.85


def _estimate_tokens(messages: list[ChatMessage]) -> int:
    total = sum(
        len(json.dumps(m.to_dict())) for m in messages
    )
    return int(total / 3.5)


async def _compact_context(ctx: AgentContext) -> None:
    messages = ctx.session.messages()
    user_assistant = [m for m in messages if m.role in ("user", "assistant")]
    if len(user_assistant) < 4:
        return
    summary_messages = [
        ChatMessage(role="system", content=ctx.system_prompt),
        *user_assistant,
        ChatMessage(
            role="user",
            content=(
                "Please summarize the conversation above into a concise context summary "
                "that preserves all important decisions, code changes, and findings. "
                "Start with '# Conversation Summary\\n\\n'."
            ),
        ),
    ]
    summary = await ctx.client.complete_simple(summary_messages)
    new_messages = [
        ChatMessage(role="system", content=f"{ctx.system_prompt}\n\n{summary}"),
    ]
    import time
    from pathlib import Path
    old_path = ctx.session.path
    pre_path = old_path.with_name(
        f"{old_path.stem}.pre-compact-{int(time.time())}.jsonl"
    )
    import shutil
    shutil.copy2(old_path, pre_path)
    ctx.session.replace_all(new_messages)
    ctx.token_estimate = _estimate_tokens(new_messages)
    console.print("[gab.accent]◆[/gab.accent] [dim]Context compacted.[/dim]", markup=True)


async def run_loop(ctx: AgentContext, initial_prompt: str | None = None) -> None:
    from gabagent.tools.registry import registry

    streaming = StreamingDisplay(console)
    tool_display = ToolCallDisplay(console)
    hooks_runner = None
    try:
        from gabagent.hooks.runner import HookRunner
        hooks_runner = HookRunner(ctx.config)
    except Exception:
        pass

    perm_engine = None
    try:
        from gabagent.permissions.engine import PermissionEngine
        perm_engine = PermissionEngine(ctx.config)
    except Exception:
        pass

    router: ModelRouter | None = None
    if not ctx.force_model and ctx.config.router.enabled:
        try:
            from gabagent.agent.router import ModelRouter
            router = ModelRouter(ctx.config)
        except Exception:
            pass

    if hooks_runner:
        await hooks_runner.run_session_start()

    # Check bridge inbox for messages from Claude Code
    try:
        from gabagent.bridge import read_inbox
        from rich.panel import Panel
        pending = read_inbox(mark_read=True)
        if pending:
            if not ctx.headless:
                for msg in pending:
                    topic = msg.get("topic", "note")
                    console.print(Panel(
                        msg["message"],
                        title=f"[bold cyan]✉ Claude Code [{topic}][/bold cyan]",
                        border_style="cyan",
                        padding=(0, 1),
                    ))
            # Inject into conversation history so the model can reference them
            combined = "\n\n".join(
                f"[Claude Code — {m.get('topic','note')}]: {m['message']}"
                for m in pending
            )
            ctx.session.append_message(ChatMessage(
                role="system",
                content=f"Messages received from Claude Code at session start:\n\n{combined}",
            ))
    except Exception:
        pass

    if initial_prompt:
        ctx.session.append_message(ChatMessage(role="user", content=initial_prompt))

    _force_input = False

    while True:
        _msgs = ctx.session.messages()
        _last_role = _msgs[-1].role if _msgs else None
        if _force_input or (not initial_prompt and _last_role not in ("user", "tool")):
            _force_input = False
            ctx.active_model = None  # reset only when starting a new user turn
            if not ctx.headless:
                from gabagent.tui.input_handler import InputHandler
                handler = InputHandler(vim_mode=ctx.config.vim_mode)
                if ctx.force_model:
                    badge = ctx.rate_limiter.forced_badge(ctx.config.model)
                else:
                    badge = ctx.rate_limiter.badge
                user_input = await handler.prompt(badge)
                if user_input is None:
                    break

                stripped = user_input.strip()
                if stripped.startswith("/"):
                    try:
                        from gabagent.slash.commands import handle_slash
                        if await handle_slash(stripped, ctx):
                            continue
                    except ImportError:
                        pass

                if not stripped:
                    continue

                if hooks_runner:
                    extra = await hooks_runner.run_user_prompt_submit(stripped)
                    if extra:
                        stripped = f"{stripped}\n\n[Hook context]\n{extra}"

                ctx.session.append_message(ChatMessage(role="user", content=stripped))
            else:
                break

        initial_prompt = None

        messages = ctx.session.messages()
        system_msg = ChatMessage(role="system", content=ctx.system_prompt)
        all_messages = [system_msg] + messages

        ctx.token_estimate = _estimate_tokens(all_messages)
        if ctx.token_estimate > ctx.config.max_context_tokens * CONTEXT_COMPACT_RATIO:
            await _compact_context(ctx)
            messages = ctx.session.messages()
            all_messages = [system_msg] + messages
        elif ctx.token_estimate > ctx.config.max_context_tokens * CONTEXT_WARN_RATIO:
            console.print(
                f"[warning]Context {ctx.token_estimate:,} tokens "
                f"({int(ctx.token_estimate / ctx.config.max_context_tokens * 100)}% full). "
                "Use /compact to compress.[/warning]",
                markup=True,
            )

        # Intent-based routing — only on fresh user turns, not tool continuations
        if router and _last_role == "user" and ctx.active_model is None:
            last_user = next(
                (m.content for m in reversed(messages) if m.role == "user" and m.content),
                "",
            )
            ctx.active_model = await router.classify_intent(last_user, ctx.client)

        tools = registry.get_schemas()

        text_buf = ""
        tool_calls: list[ToolCallSpec] = []

        try:
            streaming.start(model=ctx.active_model or ctx.config.model)
            async for chunk in ctx.client.stream_complete(
                all_messages, tools or None, model=ctx.active_model
            ):
                if isinstance(chunk, str):
                    streaming.append(chunk)
                    text_buf += chunk
                elif isinstance(chunk, list):
                    tool_calls = chunk
            streaming.stop()
        except Exception as e:
            streaming.stop()
            console.print(f"[error]API error: {e}[/error]", markup=True)
            if ctx.headless:
                break
            _force_input = True
            continue

        if text_buf:
            ctx.session.append_message(
                ChatMessage(role="assistant", content=text_buf, tool_calls=tool_calls or None)
            )

        if not tool_calls:
            if hooks_runner:
                await hooks_runner.run_stop(text_buf)
            if ctx.headless:
                break
            continue

        if not text_buf:
            ctx.session.append_message(
                ChatMessage(role="assistant", content=None, tool_calls=tool_calls)
            )

        results = await _execute_tool_calls(
            tool_calls, ctx, perm_engine, hooks_runner, tool_display, router
        )

        for tc, result in zip(tool_calls, results):
            ctx.session.append_message(
                ChatMessage(
                    role="tool",
                    content=result.to_content(),
                    tool_call_id=tc.id,
                    name=tc.name,
                )
            )


async def _execute_tool_calls(
    tool_calls: list[ToolCallSpec],
    ctx: AgentContext,
    perm_engine,
    hooks_runner,
    tool_display: ToolCallDisplay,
    router: ModelRouter | None = None,
) -> list[ToolResult]:
    from gabagent.tools.registry import registry
    from gabagent.api.models import ToolResult

    parallel: list[ToolCallSpec] = []
    serial: list[ToolCallSpec] = []

    for tc in tool_calls:
        tool_cls = registry.get_tool(tc.name)
        if tool_cls and getattr(tool_cls, "allows_parallel", True):
            parallel.append(tc)
        else:
            serial.append(tc)

    results: dict[str, ToolResult] = {}

    async def _run_one(tc: ToolCallSpec) -> ToolResult:
        import time
        try:
            args = json.loads(tc.arguments) if tc.arguments else {}
        except Exception:
            args = {}

        if perm_engine:
            from gabagent.permissions.engine import Decision
            from gabagent.permissions.prompts import interactive_approve
            decision = perm_engine.check(tc.name, args)
            if decision == Decision.DENY:
                return ToolResult(output="", error=f"Blocked by permissions: {tc.name}")
            if decision == Decision.PROMPT:
                approved = await interactive_approve(tc.name, args, ctx)
                if not approved:
                    return ToolResult(output="", error=f"Denied by user: {tc.name}")

        if hooks_runner:
            blocked, reason = await hooks_runner.run_pre_tool(tc.name, args)
            if blocked:
                return ToolResult(output="", error=f"Blocked by hook: {reason}")

        # Tool-triggered escalation
        if router and not ctx.force_model:
            override = router.check_tool_complexity(tc.name, args)
            if override and override != (ctx.active_model or router.simple_model):
                ctx.active_model = override
                console.print(
                    f"[gab.accent]▸[/gab.accent] [dim]escalating to {override} (complex tool)[/dim]", markup=True
                )

        start_time = time.time()
        tool_display.show_start(tc.name, tc.arguments)
        result = await registry.dispatch(tc.name, args, ctx)
        duration = time.time() - start_time

        # Reactive escalation
        if router and not ctx.force_model:
            override = router.check_reactive(tc.name, result.exit_code, ctx.active_model)
            if override:
                ctx.active_model = override
                console.print(
                    f"[gab.accent]▸[/gab.accent] [dim]escalating to {override} (command failed)[/dim]", markup=True
                )

        if hooks_runner:
            await hooks_runner.run_post_tool(tc.name, args, result)

        tool_display.show_result(
            tc.name,
            result.to_content(),
            is_error=not result.success,
            extra=f" ({duration:.2f}s)",
        )
        return result

    if parallel:
        async with asyncio.TaskGroup() as tg:
            tasks = {tc.id: tg.create_task(_run_one(tc)) for tc in parallel}
        for tc in parallel:
            results[tc.id] = tasks[tc.id].result()

    for tc in serial:
        results[tc.id] = await _run_one(tc)

    return [results[tc.id] for tc in tool_calls]
