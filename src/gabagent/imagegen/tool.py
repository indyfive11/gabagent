"""The `generate_image` agent tool — a self-contained brain-side capability (text + voice).

Brain owns intent (this tool); GA owns generation + local-file GC. The display seam (routing the
descriptor to a screen by room-locality) is VAC's lane and rides a separate transport — this tool just
generates, saves, and reports the file. In text/CLI mode that's the whole story: `gab "generate an image
of a sunset"` writes a PNG and tells you where.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

from gabagent.api.models import ToolResult
from gabagent.tools.base import ToolBase
from gabagent.tools.registry import registry

from .gc import gc_old_images
from .generate import generate_images

if TYPE_CHECKING:
    from gabagent.agent.context import AgentContext


@registry.register
class GenerateImageTool(ToolBase):
    name = "generate_image"
    description = (
        "Generate an image from a text prompt using the Gab/Aria image models and save it as a PNG. "
        "Returns the saved file path and dimensions. Use for 'make/draw/generate a picture of X' requests."
    )
    complexity = "complex"  # network round-trip + spends credits
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Text description of the image to generate.",
            },
            "model": {
                "type": "string",
                "description": "Optional image model id. Default from config (gpt-image-1). "
                "Cheaper alternatives: gpt-image-2, gpt-image-1-mini, image-generator.",
            },
            "size": {
                "type": "string",
                "description": "Optional image size as WxH, e.g. '1024x1024'. Default from config.",
            },
            "n": {
                "type": "integer",
                "description": "How many images to generate (default 1).",
            },
        },
        "required": ["prompt"],
    }

    async def execute(
        self,
        ctx: AgentContext,
        prompt: str,
        model: str = "",
        size: str = "",
        n: int = 1,
        **kwargs: Any,
    ) -> ToolResult:
        from gabagent.config.paths import data_dir

        cfg = ctx.config
        icfg = cfg.image
        if not icfg.enabled:
            return ToolResult(output="", error="Image generation is disabled (config: image.enabled).")
        if not (prompt or "").strip():
            return ToolResult(output="", error="A non-empty prompt is required.")
        api_key = cfg.api_key
        if not api_key:
            return ToolResult(
                output="",
                error="No Gab API key configured — image generation needs GABAI_API_KEY / api_key.",
            )

        out_dir = Path(icfg.output_dir) if icfg.output_dir else (data_dir() / "images")
        # GA owns cleanup: prune old generations before writing new ones (best-effort).
        try:
            gc_old_images(out_dir, icfg.ttl_secs)
        except Exception:
            pass

        try:
            descriptors = await generate_images(
                prompt,
                model=model or icfg.model,
                base_url=cfg.base_url,
                api_key=api_key,
                output_dir=out_dir,
                ttl_secs=icfg.ttl_secs,
                size=size or icfg.size,
                n=n,
                timeout=icfg.timeout_secs,
            )
        except Exception as e:
            return ToolResult(output="", error=f"Image generation failed: {e}")

        if not descriptors:
            return ToolResult(output="", error="Image generation returned no images.")

        # Stash the structured descriptors on the context so a future voice display-seam hook can route
        # them to a screen by room-locality (dynamic attr — no spine field needed; inert in text mode).
        try:
            ctx.image_descriptors = [d.to_dict() for d in descriptors]  # type: ignore[attr-defined]
        except Exception:
            pass

        lines = [
            f"Saved {d.path} ({d.w}×{d.h} {d.mime})" + (f" — {d.url}" if d.url else "")
            for d in descriptors
        ]
        credits = descriptors[0].credits_used
        summary = "\n".join(lines)
        if credits:
            summary += f"\n({credits} credits used)"
        return ToolResult(output=summary)
