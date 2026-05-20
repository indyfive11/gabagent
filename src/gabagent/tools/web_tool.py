from __future__ import annotations
import hashlib
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING
from gabagent.api.models import ToolResult
from .base import ToolBase
from .registry import registry

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext

_CACHE_TTL = 900  # 15 minutes
_MAX_OUTPUT = 50000


@registry.register
class WebFetchTool(ToolBase):
    name = "web_fetch"
    description = (
        "Fetch a URL and return its content as Markdown. "
        "Results are cached for 15 minutes."
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "raw": {"type": "boolean", "description": "Return raw HTML instead of Markdown"},
            "js": {"type": "boolean", "description": "Use a headless browser to render JavaScript before extracting content. Slower (~2s) but works on SPAs and JS-heavy pages."},
        },
        "required": ["url"],
    }

    async def execute(
        self, ctx: AgentContext, url: str, raw: bool = False, js: bool = False, **kwargs: Any
    ) -> ToolResult:
        from gabagent.config.paths import web_cache_dir

        cache_key = hashlib.sha256(f"{url}:js={js}".encode()).hexdigest()
        cache_path = web_cache_dir() / f"{cache_key}.txt"

        if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < _CACHE_TTL:
            return ToolResult(output=cache_path.read_text(encoding="utf-8"))

        if js:
            body = await _fetch_with_browser(url)
            if isinstance(body, Exception):
                return ToolResult(output="", error=f"Browser fetch failed: {body}")
            content_type = "text/html"
        else:
            try:
                import httpx
                headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                    ),
                }
                async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                    resp = await client.get(url, headers=headers)
                    resp.raise_for_status()
                    content_type = resp.headers.get("content-type", "")
                    body = resp.text
            except Exception as e:
                return ToolResult(output="", error=f"Fetch failed: {e}")

        if raw or "text/html" not in content_type:
            output = body[:_MAX_OUTPUT]
        else:
            try:
                import html2text
                h = html2text.HTML2Text()
                h.ignore_links = False
                h.body_width = 0
                h.ignore_images = True
                output = h.handle(body)
            except Exception:
                output = body

        if len(output) > _MAX_OUTPUT:
            output = output[:_MAX_OUTPUT] + f"\n\n...[truncated at {_MAX_OUTPUT} chars]"

        cache_path.write_text(output, encoding="utf-8")
        return ToolResult(output=output)


async def _fetch_with_browser(url: str) -> str | Exception:
    try:
        from playwright.async_api import async_playwright
    except ModuleNotFoundError:
        return ModuleNotFoundError(
            "playwright not installed — run: pip install playwright"
        )
    
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            body = await page.content()
            await browser.close()
            return body
    except Exception as e:
        # Check for missing Chromium executable
        err_msg = str(e)
        if "Executable doesn't exist" in err_msg or "chromium" in err_msg.lower():
            return Exception(
                "Chromium not downloaded — run: playwright install chromium"
            )
        return e
