"""Call the Gab/Aria `/v1/images/generations` endpoint, download the generated image to a local file,
and produce the image-seam display descriptor.

Live-verified contract (2026-07-04):
    POST {base_url}/images/generations  {"model", "prompt", "n", "size"?}
      → {"created", "data": [{"url", "revised_prompt"}], "usage": {"credits_used"}}
The image is a PUBLIC CDN PNG (cdn.gab.ai) — fetchable with no auth, so the descriptor's `url` can be
handed straight to a cross-host satellite; GA never has to serve the bytes itself. `credits_used` is the
authoritative per-call charge (the catalog `credit_cost.base_cost` is only a floor). Some models could
return `b64_json` instead of a url — handled defensively.
"""
from __future__ import annotations

import base64
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ImageDescriptor:
    """The image-seam contract (R1-agreed with VAC): what GA hands off for display.

    `path` is always present (the local file GA wrote). `url` is the public CDN url when the endpoint
    returned one (empty for a b64-only response). VAC picks `path` when the generator host == the display
    host (co-located, e.g. EM), else `url` (cross-host satellites). GA owns GC of the local file per
    `ttl_secs`; the CDN copy's retention is Gab's, not ours.
    """
    path: str
    url: str
    mime: str
    w: int
    h: int
    id: str
    ttl_secs: int
    revised_prompt: str = ""
    credits_used: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def png_dimensions(data: bytes) -> tuple[int, int]:
    """(width, height) from a PNG's IHDR chunk, or (0, 0) if not a parseable PNG. The IHDR width/height
    are big-endian uint32 at byte offsets 16 and 20 (8-byte signature + 4-byte length + b'IHDR')."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return 0, 0


def _gen_id(url: str) -> str:
    """A stable id for this image: the CDN filename stem when present, else a fresh uuid."""
    if url:
        stem = url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
        if stem.lower().endswith(".png"):
            stem = stem[:-4]
        if stem:
            return stem
    return uuid.uuid4().hex


async def generate_images(
    prompt: str,
    *,
    model: str,
    base_url: str,
    api_key: str,
    output_dir: str | Path,
    ttl_secs: int,
    size: str = "",
    n: int = 1,
    timeout: float = 120.0,
) -> list[ImageDescriptor]:
    """Generate `n` image(s), write each PNG under `output_dir`, and return their descriptors.

    RAISES on API/network/download error (the tool wrapper turns that into a ToolResult error). The gab
    api_key authorizes the POST; the CDN GET needs no auth (public), so it goes out header-free.
    """
    import httpx

    body: dict[str, Any] = {"model": model, "prompt": prompt, "n": max(1, int(n))}
    if size:
        body["size"] = size

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    url = f"{base_url.rstrip('/')}/images/generations"

    descriptors: list[ImageDescriptor] = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers={"Authorization": f"Bearer {api_key}"}, json=body)
        resp.raise_for_status()
        payload = resp.json()
        credits = int((payload.get("usage") or {}).get("credits_used", 0) or 0)

        for rec in payload.get("data", []) or []:
            img_url = rec.get("url", "") or ""
            b64 = rec.get("b64_json") or ""
            if img_url:
                dl = await client.get(img_url)   # public CDN — no auth header
                dl.raise_for_status()
                data = dl.content
            elif b64:
                data = base64.b64decode(b64)
            else:
                continue

            gen_id = _gen_id(img_url)
            w, h = png_dimensions(data)
            dest = out_dir / f"{gen_id}.png"
            dest.write_bytes(data)
            descriptors.append(
                ImageDescriptor(
                    path=str(dest),
                    url=img_url,
                    mime="image/png",
                    w=w,
                    h=h,
                    id=gen_id,
                    ttl_secs=ttl_secs,
                    revised_prompt=rec.get("revised_prompt", "") or "",
                    credits_used=credits,
                )
            )
    return descriptors
