"""Real artwork for picture requests, made with OpenAI's image model.

"Draw two pelicans riding a bicycle through New York" used to come out as a
few rectangles. Now the picture itself is generated, saved where the Stage
can load it (/stage-assets/generated/), and Mika codes a living scene around
it on stream: slow camera moves, particles, light, title text.

    VR_ART=0                    turn it off (the scene is drawn in code instead)
    VR_IMAGE_MODEL              first model to try (default gpt-image-1-mini)
    VR_IMAGE_QUALITY            low | medium | high (default low, the cheapest)

The models are tried in order, so an account without access to one still
gets a picture from the next. Nothing here ever raises into the stream.
"""

from __future__ import annotations

import base64
import os
import re
import secrets
import time
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger

from .capabilities import ASSETS_DIR

GENERATED_DIR = ASSETS_DIR / "generated"
URL_PREFIX = "/stage-assets/generated/"
API = "https://api.openai.com/v1/images/generations"
MAX_BYTES = 2_800_000  # the publisher refuses files over 3 MB

STYLE = (
    "Vibrant, detailed digital illustration in a polished anime-inspired style, "
    "cinematic lighting, rich colours, wide 16:9 composition with the subject "
    "clearly in frame, lively background. No text, no letters, no watermark, "
    "no logos."
)

# The image model has never seen our characters: describe them.
CHARACTERS = {
    "mika": (
        "Mika, a cheerful anime witch girl with short orange-blonde hair, big "
        "blue eyes, a navy witch hat with a white bunny charm and a red feather, "
        "a white hoodie under a navy and orange coat, holding a magic brush"
    ),
    "luna": (
        "Luna, a calm anime girl with long brown twin tails tied with red "
        "ribbons, blue eyes, a cream cardigan over a navy sailor school "
        "uniform with a blue ribbon"
    ),
}

# Changes that only touch the code's effects or words keep the same picture.
CONTENT_WORDS = re.compile(
    r"\b(person|people|man|woman|girl|boy|cat|dog|car|cars|taxi|taxis|bird|pelicans?|"
    r"house|city|street|tree|dragon|robot|character|mika|luna|building|animal|"
    r"scene|picture|image|background|ocean|sea|forest|mountain|bicycle|bike|"
    r"portrait|face|outfit|style)\b",
    re.IGNORECASE,
)
EFFECT_WORDS = re.compile(
    r"\b(text|title|font|words?|sparkl\w*|glitter\w*|stars?|rain|snow|fireworks?|"
    r"particles?|glow|faster|slower|speed|bigger|smaller|zoom|animate|animation|"
    r"move|moving|brighter|darker|colou?r of the (text|title))\b",
    re.IGNORECASE,
)


def art_enabled() -> bool:
    return os.environ.get("VR_ART", "1").strip().lower() not in ("0", "false", "no") and bool(
        os.environ.get("OPENAI_API_KEY", "").strip()
    )



def needs_new_art(instruction: str) -> bool:
    """A change to an existing picture: True when the picture itself changes."""
    text = str(instruction or "")
    return not (EFFECT_WORDS.search(text) and not CONTENT_WORDS.search(text))


def art_prompt(request: str, extra: str = "") -> str:
    text = " ".join(f"{request} {extra}".split())[:900]
    for key, description in CHARACTERS.items():
        if re.search(rf"\b{key}\b", text, re.IGNORECASE):
            text += f" ({description})"
    return f"{text}\n\n{STYLE}"


def _models() -> list[str]:
    first = os.environ.get("VR_IMAGE_MODEL", "").strip() or "gpt-image-1-mini"
    chain = [first, "gpt-image-1-mini", "gpt-image-1", "dall-e-3"]
    return list(dict.fromkeys(m for m in chain if m))


def _body(model: str, prompt: str) -> dict:
    if model.startswith("dall-e"):
        return {
            "model": model,
            "prompt": prompt[:3900],
            "size": "1792x1024",
            "quality": "standard",
            "n": 1,
            "response_format": "b64_json",
        }
    return {
        "model": model,
        "prompt": prompt[:3900],
        "size": "1536x1024",
        "quality": os.environ.get("VR_IMAGE_QUALITY", "low").strip() or "low",
        "n": 1,
        "output_format": "jpeg",
        "output_compression": 85,
    }


def _shrink(data: bytes) -> tuple[bytes, str]:
    """Keep it under the publisher's size limit (JPEG when Pillow exists)."""
    if len(data) <= MAX_BYTES and data[:3] == b"\xff\xd8\xff":
        return data, "jpg"
    try:
        from io import BytesIO

        from PIL import Image

        image = Image.open(BytesIO(data)).convert("RGB")
        image.thumbnail((1600, 1600))
        out = BytesIO()
        image.save(out, "JPEG", quality=84, optimize=True)
        return out.getvalue(), "jpg"
    except Exception:
        ext = "png" if data[:4] == b"\x89PNG" else "jpg"
        return data, ext


def new_art_url() -> tuple[str, str]:
    """(file stem, url) reserved before the picture exists, so the code
    writer can use the path while the picture is still being made."""
    stem = f"art-{int(time.time())}-{secrets.token_hex(3)}"
    return stem, f"{URL_PREFIX}{stem}.jpg"


async def generate_art(prompt: str, stem: str, client: Optional[httpx.AsyncClient] = None) -> str:
    """Make the picture and save it as <stem>.jpg. Returns its URL, or ""."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        return ""
    owns = client is None
    client = client or httpx.AsyncClient(timeout=120)
    started = time.perf_counter()
    try:
        for model in _models():
            try:
                response = await client.post(
                    API,
                    headers={"Authorization": f"Bearer {key}"},
                    json=_body(model, prompt),
                )
            except httpx.HTTPError as exc:
                logger.warning(f"Art: {model} unreachable: {exc}")
                continue
            if response.status_code >= 400:
                logger.warning(
                    f"Art: {model} refused ({response.status_code}): {response.text[:200]}"
                )
                continue
            try:
                b64 = response.json()["data"][0]["b64_json"]
                data, _ext = _shrink(base64.b64decode(b64))
            except Exception as exc:
                logger.warning(f"Art: {model} returned no picture: {exc}")
                continue
            GENERATED_DIR.mkdir(parents=True, exist_ok=True)
            path = GENERATED_DIR / f"{stem}.jpg"
            path.write_bytes(data)
            logger.info(
                f"Art made with {model} in {time.perf_counter() - started:.1f}s "
                f"({len(data) // 1024} KB): {path.name}"
            )
            return f"{URL_PREFIX}{stem}.jpg"
        return ""
    finally:
        if owns:
            await client.aclose()


def fallback_art() -> str:
    """A built-in background when no picture could be made."""
    for name in ("backgrounds/night-sky.svg", "backgrounds/space-nebula.svg"):
        if (ASSETS_DIR / name).is_file():
            return f"/stage-assets/{name}"
    return ""


def art_exists(url: str) -> bool:
    if not url.startswith(URL_PREFIX):
        return False
    return (GENERATED_DIR / Path(url[len(URL_PREFIX):]).name).is_file()
