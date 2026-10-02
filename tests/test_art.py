"""Picture requests get a real painting (mocked here) and a living scene."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from open_llm_vtuber.room import art  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 2000


def test_models_are_tried_in_order_and_the_picture_is_saved(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("VR_IMAGE_MODEL", raising=False)
    monkeypatch.setattr(art, "GENERATED_DIR", tmp_path)
    tried = []

    def handler(request):
        body = json.loads(request.content)
        tried.append(body["model"])
        if body["model"] == "gpt-image-1-mini":
            return httpx.Response(403, json={"error": "no access"})
        assert "Mika, a cheerful anime witch girl" in body["prompt"]
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(JPEG).decode()}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await art.generate_art(art.art_prompt("draw Mika in space"), "art-x", client)

    url = asyncio.run(go())
    assert url == "/stage-assets/generated/art-x.jpg"
    assert tried[:2] == ["gpt-image-1-mini", "gpt-image-1"]
    assert (tmp_path / "art-x.jpg").read_bytes() == JPEG


def test_no_key_or_all_refused_gives_no_picture(monkeypatch, tmp_path):
    monkeypatch.setattr(art, "GENERATED_DIR", tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert asyncio.run(art.generate_art("x", "a")) == ""
    assert not art.art_enabled()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    async def go():
        transport = httpx.MockTransport(lambda r: httpx.Response(400, json={}))
        async with httpx.AsyncClient(transport=transport) as client:
            return await art.generate_art("x", "b", client)

    assert asyncio.run(go()) == ""
    assert art.fallback_art().startswith("/stage-assets/")


def test_only_real_picture_changes_paint_again():
    assert not art.needs_new_art("add glittering stars")
    assert not art.needs_new_art("make the title bigger")
    assert art.needs_new_art("make it a busy street with taxis")
    assert art.needs_new_art("now draw a dragon")


def test_pollinations_is_tried_first_when_it_has_a_key(monkeypatch, tmp_path):
    monkeypatch.setattr(art, "GENERATED_DIR", tmp_path)
    monkeypatch.setenv("POLLINATIONS_API_KEY", "sk_poll")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    seen = []

    def handler(request):
        seen.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer sk_poll"
        return httpx.Response(200, content=JPEG, headers={"content-type": "image/jpeg"})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await art.generate_art("pelicans on a bicycle", "p1", client)

    assert asyncio.run(go()) == "/stage-assets/generated/p1.jpg"
    assert seen[0].startswith("https://gen.pollinations.ai/image/") and "model=flux" in seen[0]
    assert art.art_enabled()
