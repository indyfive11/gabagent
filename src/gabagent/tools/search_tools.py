from __future__ import annotations
import subprocess
import json
from pathlib import Path
from typing import Any, TYPE_CHECKING
from gabagent.api.models import ToolResult
from .base import ToolBase
from .registry import registry

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


@registry.register
class GrepTool(ToolBase):
    name = "grep"
    description = (
        "Search for a pattern in files using ripgrep. "
        "Returns matching lines with file paths and line numbers."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern to search for"},
            "path": {"type": "string", "description": "Directory or file to search (default: project root)"},
            "glob": {"type": "string", "description": "File glob filter (e.g. '*.py')"},
            "case_sensitive": {"type": "boolean", "description": "Case sensitive search (default: false)"},
            "context_lines": {"type": "integer", "description": "Lines of context around matches"},
        },
        "required": ["pattern"],
    }

    async def execute(
        self,
        ctx: AgentContext,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        case_sensitive: bool = False,
        context_lines: int = 0,
        **kwargs: Any,
    ) -> ToolResult:
        search_path = Path(path) if path else ctx.cwd
        if not search_path.is_absolute():
            search_path = ctx.cwd / search_path

        cmd = ["rg", "--line-number", "--no-heading", "--color=never"]
        if not case_sensitive:
            cmd.append("--ignore-case")
        if glob:
            cmd += ["--glob", glob]
        if context_lines:
            cmd += ["-C", str(context_lines)]
        cmd += [pattern, str(search_path)]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            output = result.stdout
            if len(output) > 20000:
                output = output[:20000] + "\n...[truncated]"
            if not output.strip():
                return ToolResult(output="(no matches)")
            return ToolResult(output=output)
        except FileNotFoundError:
            return ToolResult(output="", error="rg (ripgrep) not found")
        except subprocess.TimeoutExpired:
            return ToolResult(output="", error="Search timed out")
        except Exception as e:
            return ToolResult(output="", error=str(e))


@registry.register
class WebSearchTool(ToolBase):
    name = "web_search"
    description = "Search the web for information. Returns top results with titles, URLs, and snippets."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query"},
        },
        "required": ["query"],
    }

    async def execute(self, ctx: AgentContext, query: str, **kwargs: Any) -> ToolResult:
        import hashlib
        import time
        from gabagent.config.paths import web_cache_dir

        cache_key = hashlib.sha256(f"search:{query}".encode()).hexdigest()
        cache_path = web_cache_dir() / f"{cache_key}.txt"
        if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < 300:
            return ToolResult(output=cache_path.read_text())

        # Try ddgs library (handles bot detection reliably)
        try:
            import asyncio
            from ddgs import DDGS

            def _search() -> list[str]:
                with DDGS() as ddgs:
                    hits = list(ddgs.text(query, max_results=10))
                lines = []
                for r in hits:
                    title = r.get("title", "")
                    url = r.get("href", "")
                    snippet = r.get("body", "")[:200]
                    lines.append(f"- [{title}]({url})\n  {snippet}")
                return lines

            results = await asyncio.get_event_loop().run_in_executor(None, _search)
            output = "\n".join(results)
            if output:
                cache_path.write_text(output)
            return ToolResult(output=output or "(no results)")
        except Exception as e:
            pass

        # SearxNG fallback
        searxng = ctx.config.searxng_url
        if searxng:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(
                        f"{searxng}/search",
                        params={"q": query, "format": "json"},
                    )
                    data = resp.json()
                    hits = data.get("results", [])[:10]
                    lines = [f"- [{r.get('title','')}]({r.get('url','')})\n  {r.get('content','')[:200]}" for r in hits]
                    output = "\n".join(lines)
                    if output:
                        cache_path.write_text(output)
                    return ToolResult(output=output or "(no results)")
            except Exception as e2:
                return ToolResult(output="", error=f"Search failed: ddgs and searxng both unavailable")

        return ToolResult(output="", error="Search unavailable: install ddgs or configure searxng_url")


def _parse_ddg_html(html: str) -> list[str]:
    import re
    results = []
    # Extract result blocks
    blocks = re.findall(r'<div class="result.*?</div>\s*</div>', html, re.DOTALL)
    for block in blocks[:15]:
        title_m = re.search(r'<a[^>]*class="result__a"[^>]*>(.*?)</a>', block, re.DOTALL)
        url_m = re.search(r'href="([^"]*)"', block)
        snippet_m = re.search(r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', block, re.DOTALL)
        if title_m:
            title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip()
            url = url_m.group(1) if url_m else ""
            snippet = re.sub(r'<[^>]+>', '', snippet_m.group(1)).strip() if snippet_m else ""
            results.append(f"- [{title}]({url})\n  {snippet[:200]}")
    return results
