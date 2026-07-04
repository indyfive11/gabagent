"""Tests for the image-generation plugin (Gab/Aria /v1/images/generations → generate_image tool)."""
import base64
import struct
import time
import types

import httpx
import pytest

from gabagent.config.models import GabAgentConfig
from gabagent.imagegen import gc_old_images, png_dimensions
from gabagent.imagegen import generate as G
from gabagent.imagegen.generate import ImageDescriptor, generate_images
from gabagent.imagegen.tool import GenerateImageTool


def _png(w: int, h: int) -> bytes:
    """A byte string whose leading PNG signature + IHDR chunk encode (w, h). Enough for the dimension
    parser and to be written as a file; not a fully-rendered PNG."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x06\x00\x00\x00"
    return sig + ihdr


# ── fake httpx client ────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, *, json_data=None, content=b""):
        self._json = json_data
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return self._json


class _FakeClient:
    def __init__(self, post_json, image_bytes):
        self._post_json = post_json
        self._image_bytes = image_bytes
        self.posted = None
        self.gets: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, headers=None, json=None):
        self.posted = {"url": url, "headers": headers, "json": json}
        return _Resp(json_data=self._post_json)

    async def get(self, url, headers=None):
        self.gets.append(url)
        return _Resp(content=self._image_bytes)


def _install_fake(monkeypatch, post_json, image_bytes):
    client = _FakeClient(post_json, image_bytes)
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: client)
    return client


# ── png_dimensions ───────────────────────────────────────────────────────────

def test_png_dimensions_parses_ihdr():
    assert png_dimensions(_png(1024, 768)) == (1024, 768)


def test_png_dimensions_rejects_non_png():
    assert png_dimensions(b"not a png at all really") == (0, 0)
    assert png_dimensions(b"") == (0, 0)


# ── generate_images ──────────────────────────────────────────────────────────

async def test_generate_downloads_url_and_writes_file(tmp_path, monkeypatch):
    url = "https://cdn.gab.ai/users/x/uploads/abc123.png"
    post_json = {
        "created": 1,
        "data": [{"url": url, "revised_prompt": "a red apple"}],
        "usage": {"credits_used": 5},
    }
    client = _install_fake(monkeypatch, post_json, _png(512, 512))
    descs = await generate_images(
        "a red apple", model="gpt-image-1", base_url="https://gab.ai/v1",
        api_key="gab_k", output_dir=tmp_path, ttl_secs=3600, size="512x512",
    )
    assert len(descs) == 1
    d = descs[0]
    assert d.id == "abc123"
    assert d.url == url and d.mime == "image/png"
    assert (d.w, d.h) == (512, 512)
    assert d.revised_prompt == "a red apple" and d.credits_used == 5
    assert d.ttl_secs == 3600
    # file actually written, named by gen id
    written = tmp_path / "abc123.png"
    assert written.exists() and written.read_bytes() == _png(512, 512)
    # request shape: model/prompt/n/size, bearer auth on the POST, no auth header on the CDN GET
    assert client.posted["json"] == {"model": "gpt-image-1", "prompt": "a red apple", "n": 1, "size": "512x512"}
    assert client.posted["headers"]["Authorization"] == "Bearer gab_k"
    assert client.gets == [url]


async def test_generate_omits_size_when_empty(tmp_path, monkeypatch):
    client = _install_fake(
        monkeypatch,
        {"data": [{"url": "https://cdn.gab.ai/u/z.png"}], "usage": {"credits_used": 2}},
        _png(256, 256),
    )
    await generate_images("x", model="gpt-image-2", base_url="https://gab.ai/v1",
                          api_key="k", output_dir=tmp_path, ttl_secs=10)
    assert "size" not in client.posted["json"]


async def test_generate_handles_b64_response(tmp_path, monkeypatch):
    raw = _png(64, 64)
    post_json = {"data": [{"b64_json": base64.b64encode(raw).decode()}], "usage": {"credits_used": 1}}
    _install_fake(monkeypatch, post_json, b"UNUSED")
    descs = await generate_images("x", model="m", base_url="https://gab.ai/v1",
                                  api_key="k", output_dir=tmp_path, ttl_secs=10)
    assert len(descs) == 1
    d = descs[0]
    assert d.url == "" and (d.w, d.h) == (64, 64)       # no CDN url on a b64 response
    assert (tmp_path / f"{d.id}.png").read_bytes() == raw


def test_descriptor_to_dict_shape():
    d = ImageDescriptor(path="/a.png", url="u", mime="image/png", w=1, h=2, id="i", ttl_secs=9,
                        revised_prompt="p", credits_used=3)
    assert d.to_dict() == {
        "path": "/a.png", "url": "u", "mime": "image/png", "w": 1, "h": 2,
        "id": "i", "ttl_secs": 9, "revised_prompt": "p", "credits_used": 3,
    }


# ── gc ───────────────────────────────────────────────────────────────────────

def test_gc_removes_only_old_files(tmp_path):
    old = tmp_path / "old.png"
    new = tmp_path / "new.png"
    old.write_bytes(b"x")
    new.write_bytes(b"y")
    past = time.time() - 10_000
    import os
    os.utime(old, (past, past))
    removed = gc_old_images(tmp_path, ttl_secs=3600)
    assert removed == 1
    assert not old.exists() and new.exists()


def test_gc_disabled_when_ttl_zero(tmp_path):
    p = tmp_path / "a.png"
    p.write_bytes(b"x")
    import os
    past = time.time() - 10_000
    os.utime(p, (past, past))
    assert gc_old_images(tmp_path, ttl_secs=0) == 0
    assert p.exists()


def test_gc_missing_dir_is_zero(tmp_path):
    assert gc_old_images(tmp_path / "nope", ttl_secs=3600) == 0


# ── the tool ─────────────────────────────────────────────────────────────────

def _ctx(tmp_path, **over):
    cfg = GabAgentConfig()
    cfg.api_key = over.pop("api_key", "gab_k")
    cfg.base_url = "https://gab.ai/v1"
    cfg.image.output_dir = str(tmp_path)
    for k, v in over.items():
        setattr(cfg.image, k, v)
    return types.SimpleNamespace(config=cfg)


async def test_tool_disabled_returns_error(tmp_path):
    r = await GenerateImageTool().execute(_ctx(tmp_path, enabled=False), prompt="x")
    assert r.error and "disabled" in r.error


async def test_tool_requires_api_key(tmp_path):
    r = await GenerateImageTool().execute(_ctx(tmp_path, api_key=""), prompt="x")
    assert r.error and "API key" in r.error


async def test_tool_requires_prompt(tmp_path):
    r = await GenerateImageTool().execute(_ctx(tmp_path), prompt="   ")
    assert r.error and "prompt" in r.error


async def test_tool_success_reports_path_and_stashes_descriptor(tmp_path, monkeypatch):
    desc = ImageDescriptor(path=str(tmp_path / "abc.png"), url="https://cdn.gab.ai/u/abc.png",
                           mime="image/png", w=1024, h=1024, id="abc", ttl_secs=3600,
                           revised_prompt="a cat", credits_used=5)

    async def fake_gen(prompt, **kw):
        assert prompt == "a cat"
        assert kw["model"] == "gpt-image-1"    # default from config
        return [desc]

    monkeypatch.setattr("gabagent.imagegen.tool.generate_images", fake_gen)
    ctx = _ctx(tmp_path)
    r = await GenerateImageTool().execute(ctx, prompt="a cat")
    assert r.error is None
    assert str(tmp_path / "abc.png") in r.output
    assert "1024×1024" in r.output and "5 credits" in r.output
    # structured descriptor stashed for the future voice display seam
    assert ctx.image_descriptors == [desc.to_dict()]


async def test_tool_voice_mode_enqueues_display_item(tmp_path, monkeypatch):
    desc = ImageDescriptor(path=str(tmp_path / "abc.png"), url="https://cdn.gab.ai/u/abc.png",
                           mime="image/png", w=1024, h=1024, id="abc", ttl_secs=3600,
                           revised_prompt="a cat", credits_used=5)

    async def fake_gen(prompt, **kw):
        return [desc]

    monkeypatch.setattr("gabagent.imagegen.tool.generate_images", fake_gen)
    from gabagent.voice import announce_store as A
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    ctx = _ctx(tmp_path)
    ctx.voice_mode = True
    ctx.voice_session = types.SimpleNamespace(session_id="room-em")
    r = await GenerateImageTool().execute(ctx, prompt="a cat")
    assert r.error is None

    out = A.poll("room-em", now=1.0)
    assert len(out) == 1
    assert out[0]["job_id"] == "img-abc"
    assert out[0]["text"] == ""                       # display-only, no spoken line
    assert out[0]["display"]["url"] == "https://cdn.gab.ai/u/abc.png"
    assert out[0]["display"]["path"] == str(tmp_path / "abc.png")


async def test_tool_text_mode_does_not_enqueue(tmp_path, monkeypatch):
    desc = ImageDescriptor(path=str(tmp_path / "x.png"), url="u", mime="image/png", w=1, h=1,
                           id="x", ttl_secs=60)

    async def fake_gen(prompt, **kw):
        return [desc]

    monkeypatch.setattr("gabagent.imagegen.tool.generate_images", fake_gen)
    from gabagent.voice import announce_store as A
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data2"))

    ctx = _ctx(tmp_path)   # no voice_mode attr → text mode
    await GenerateImageTool().execute(ctx, prompt="x")
    assert A.poll("anyone", now=1.0) == []             # nothing enqueued in text mode


async def test_tool_wraps_generation_error(tmp_path, monkeypatch):
    async def boom(prompt, **kw):
        raise RuntimeError("upstream 500")

    monkeypatch.setattr("gabagent.imagegen.tool.generate_images", boom)
    r = await GenerateImageTool().execute(_ctx(tmp_path), prompt="x")
    assert r.error and "upstream 500" in r.error


async def test_tool_registered_in_registry():
    from gabagent.tools.registry import registry
    import gabagent.imagegen.tool  # noqa: F401  (ensure import side-effect)
    assert registry.get_tool("generate_image") is GenerateImageTool
