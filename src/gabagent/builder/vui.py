"""Builder Voice User Interface — deterministic, no-LLM control verbs anchored on "builder"/"build".

`detect(text)` recognizes the builder control phrases so they never hit the model (always recognized,
instant, latency-proof — same contract as voice/commands.py meta-commands). `handle()` executes the
verb against the project registry / ops and emits the spoken reply. The DISPATCH verb ("start a builder
task …") is deliberately NOT here — it needs the LLM to parse the task and goes through send_to_builder.

Every phrase is anchored on builder/build/graduate so it can't swallow a media control or a dispatch.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from gabagent.voice.commands import MetaCommand

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

# Each pattern → verb; an optional named group `name` captures a project/target argument. Order matters:
# the more specific phrases are listed first (working-on before status, named-new before switch).
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bbuilder\s+help\b|\bhelp\s+with\s+(?:the\s+)?builder\b"
                r"|\bwhat\s+can\s+the\s+builder\s+do\b", re.I), "help"),
    (re.compile(r"\bwhat(?:'?s| is|\s+is)\s+the\s+builder\s+working\s+on\b"
                r"|\bwhat'?s?\s+(?:the\s+builder\s+)?building\b", re.I), "working"),
    (re.compile(r"\bnew\s+builder\s+project\b(?:\s+(?:called|named)\s+(?P<name>.+))?", re.I), "new"),
    # switch BEFORE list, so "open builder project snake" doesn't trip the bare "builder projects" list rule.
    (re.compile(r"\b(?:switch\s+(?:the\s+)?builder\s+to"
                r"|builder[,]?\s+(?:switch\s+to|work\s+on|open)"
                r"|open\s+builder\s+project)\s+(?P<name>.+)", re.I), "switch"),
    (re.compile(r"\b(?:list|show|what)\b[^.]*\bbuilder\s+projects?\b"
                r"|\bbuilder\s+projects?\b|\bwhat'?s\s+in\s+the\s+builder\b", re.I), "list"),
    # graduate: tolerate tense (graduate/graduated/graduating), optional "it/this", and "as|to <name>".
    (re.compile(r"\bgraduat(?:e|ed|ing)\b(?:\s+(?:it|this|the\s+(?:project|build)))?"
                r"(?:\s+(?:as|to)\s+(?P<name>.+))?", re.I), "graduate"),
    (re.compile(r"\b(?:builder|build)\s+status\b|\bhow(?:'?s|\s+is|\s+are)\s+the\s+build\b"
                r"|\bis\s+the\s+build(?:er)?\s+done\b", re.I), "status"),
    # cancel: allow determiners between the verb and "build(er)" ("cancel all the builder tasks").
    (re.compile(r"\b(?:cancel|stop|abort|kill)\s+(?:(?:all|that|the|my|any)\s+)*"
                r"build(?:er)?\b", re.I), "cancel"),
    (re.compile(r"\bdiscard\s+(?:that|the|this)\s+build\b"
                r"|\bthrow\s+away\s+(?:that|the)\s+build\b|\brevert\s+(?:the\s+)?build\b", re.I), "discard"),
]


def detect(text: str) -> MetaCommand | None:
    t = (text or "").strip()
    for pat, verb in _PATTERNS:
        m = pat.search(t)
        if m:
            arg = ""
            if "name" in pat.groupindex:
                from gabagent.voice.spoken_tokens import normalize_name
                arg = (m.groupdict().get("name") or "").strip().strip(".?!,").strip()
                # Brain owns spoken assembly: "snake dash game" → "snake-game" before slugify/resolve.
                arg = normalize_name(arg)
            return MetaCommand("builder", verb, arg)
    return None


# -- handlers --------------------------------------------------------------

def _scratch_root(ctx: AgentContext) -> str:
    return (getattr(ctx.config, "builder_scratch_root", "") or "").strip()


def _graduate_root(ctx: AgentContext) -> str:
    return (getattr(ctx.config, "builder_graduate_root", "") or "").strip()


def _running(project_path: str | None = None) -> list[dict]:
    from gabagent.builder import store
    jobs = [j for j in store.list_jobs() if j.get("status") in ("queued", "running")]
    if project_path:
        jobs = [j for j in jobs if j.get("project") == project_path]
    return jobs


def _names_phrase(items: list[dict]) -> str:
    names = [p["name"] for p in items]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _auto_name(scratch_root: str) -> str:
    from gabagent.builder import projects
    existing = {p["name"] for p in projects.list_all(scratch_root)}
    if "scratch" not in existing:
        return "scratch"
    i = 2
    while f"scratch-{i}" in existing:
        i += 1
    return f"scratch-{i}"


def _reply_list(ctx: AgentContext) -> str:
    from gabagent.builder import projects
    items = projects.list_all(_scratch_root(ctx))
    if not items:
        return "You don't have any builder projects yet. Say 'new builder project' and a name to start one."
    act = projects.active()
    parts = []
    for p in items[:6]:
        tag = " — current" if act and p["name"] == act["name"] else (" — graduated" if p.get("graduated") else "")
        desc = p.get("description")
        parts.append(f"{p['name']}{tag}" + (f": {desc}" if desc else ""))
    n = len(items)
    head = f"You have {n} builder project{'s' if n != 1 else ''}: "
    more = "" if n <= 6 else " …and more."
    return head + "; ".join(parts) + "." + more


def _reply_new(ctx: AgentContext, name: str) -> str:
    from gabagent.builder import projects
    root = _scratch_root(ctx)
    if not root:
        return "No builder sandbox is set up, so I can't create a project. Set a scratch folder first."
    chosen = name or _auto_name(root)
    try:
        rec = projects.new_sandbox_project(chosen, root)
    except Exception as e:
        return f"I couldn't create the project: {e}"
    return (f"New builder project {rec['name']} — it's the current one now. "
            f"Say 'start a builder task' to build in it.")


def _reply_switch(ctx: AgentContext, query: str) -> str:
    from gabagent.builder import projects
    if not query:
        return "Which project should I switch to? Say 'list builder projects' to hear them."
    status, payload = projects.resolve(query, _scratch_root(ctx))
    if status == "ok":
        projects.set_active(payload["name"])
        desc = payload.get("description")
        return f"Now on builder project {payload['name']}" + (f" — {desc}." if desc else ".")
    if status == "ambiguous":
        return f"Did you mean {_names_phrase(payload)}?"
    if payload:  # none, but projects exist
        return f"I don't have a builder project called {query}. You have {_names_phrase(payload)}."
    return "You don't have any builder projects yet."


def _reply_graduate(ctx: AgentContext, name: str) -> str:
    from gabagent.builder import graduate, projects
    act = projects.active()
    if act is None:
        return "There's no current builder project to graduate."
    grad_root = _graduate_root(ctx)
    if not grad_root:
        return "No graduation folder is configured, so I can't promote it yet."
    if not name:  # suggest → confirm
        sug = graduate.suggest_name(act["path"])
        dest = Path(grad_root).expanduser() / sug
        return (f"I'd call it {sug} and put it at {dest}. "
                f"Say 'graduate it as {sug}' to confirm, or give it another name.")
    try:
        final, dest = graduate.graduate(ctx, name=name)
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"I couldn't graduate it: {e}"
    return f"Graduated — {final} now lives at {dest}, and I'll keep building there without asking."


def _reply_status(ctx: AgentContext) -> str:
    from gabagent.builder import store
    running = _running()
    if running:
        j = running[-1]
        proj = Path(j.get("project", "")).name or "the project"
        return f"Still building in {proj}: {j.get('task', '')[:70]}."
    done = [j for j in store.list_jobs() if j.get("status") in ("done", "failed")]
    if not done:
        return "Nothing's building, and there are no builds yet."
    j = done[-1]
    proj = Path(j.get("project", "")).name or "the project"
    if j.get("status") == "failed":
        return f"The last build in {proj} failed. Say 'check builder' for details."
    nf = len(j.get("files_changed") or [])
    return f"The last build in {proj} finished — {nf} file{'s' if nf != 1 else ''} changed."


def _reply_working(ctx: AgentContext) -> str:
    from gabagent.builder import projects
    act = projects.active()
    base = (f"The builder's current project is {act['name']}." if act
            else "There's no current builder project.")
    running = _running(act["path"] if act else None) or _running()
    if running:
        return base + f" It's building: {running[-1].get('task', '')[:70]}."
    return base + " Nothing's building right now."


def _reply_cancel(ctx: AgentContext) -> str:
    from gabagent.builder import ops
    status, _ = ops.cancel_running()
    return {
        "cancelled": "Okay, I stopped the build.",
        "none": "Nothing's building right now.",
        "error": "I tried to stop the build but couldn't confirm it stopped.",
    }.get(status, "Okay.")


def _reply_discard(ctx: AgentContext) -> str:
    from gabagent.builder import ops
    status, name = ops.discard_active()
    return {
        "discarded": f"Discarded the uncommitted changes in {name}.",
        "clean": f"Nothing to discard in {name} — it's already clean.",
        "no-active": "There's no current builder project.",
        "not-git": f"{name} isn't a git project, so there's nothing to revert.",
        "error": "I couldn't discard the changes.",
    }.get(status, "Okay.")


_HELP = ("Builder commands: 'start a builder task' to build; 'new builder project' to start one; "
         "'list builder projects'; 'switch builder to' a name; 'graduate it' when it's ready; "
         "'builder status'; and 'cancel the build'.")


def respond(ctx: AgentContext, verb: str, arg: str = "") -> str:
    """Execute a builder control verb and return the spoken/typed reply. Shared by the voice handler and
    the manage_builder tool so both paths behave identically."""
    if verb == "list":
        return _reply_list(ctx)
    if verb == "new":
        return _reply_new(ctx, arg)
    if verb == "switch":
        return _reply_switch(ctx, arg)
    if verb == "graduate":
        return _reply_graduate(ctx, arg)
    if verb == "status":
        return _reply_status(ctx)
    if verb == "working":
        return _reply_working(ctx)
    if verb == "cancel":
        return _reply_cancel(ctx)
    if verb == "discard":
        return _reply_discard(ctx)
    if verb == "help":
        return _HELP
    return "I didn't catch that builder command."


async def handle(ctx: AgentContext, mc: MetaCommand, emit) -> None:
    """Execute a builder verb and emit the spoken reply. Caller (_handle_meta) emits the trailing done()."""
    from gabagent.voice import events
    await emit(events.token(respond(ctx, mc.value, mc.arg)))
