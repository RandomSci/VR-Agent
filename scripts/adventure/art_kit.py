"""Small drawing kit for the procedural world art (Pillow + numpy).

Everything is drawn at SS times the final size and downsampled, which gives
clean anti-aliased edges. Shapes are filled with vertical gradients and get a
soft darker outline, a cel-style highlight and a contact shadow, so the props
sit next to the Live2D characters without looking like debug rectangles.
"""

from __future__ import annotations

import math
import random
from typing import Callable, Iterable, Sequence

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

SS = 4  # supersampling factor

Color = tuple[int, int, int] | tuple[int, int, int, int]


def rgba(c: Color, a: int | None = None) -> tuple[int, int, int, int]:
    if len(c) == 4:
        return c if a is None else (c[0], c[1], c[2], a)  # type: ignore[index]
    return (c[0], c[1], c[2], 255 if a is None else a)


def mix(a: Color, b: Color, t: float) -> tuple[int, int, int]:
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))  # type: ignore[return-value]


def shade(c: Color, f: float) -> tuple[int, int, int]:
    """f < 1 darker, f > 1 lighter (towards white)."""
    if f <= 1:
        return tuple(int(v * f) for v in c[:3])  # type: ignore[return-value]
    return mix(c, (255, 255, 255), min(1.0, f - 1))


class Canvas:
    """RGBA canvas drawn at SS resolution; ``final()`` downsamples."""

    def __init__(self, w: int, h: int, ss: int = SS, pad: int = 0):
        self.pad = pad
        self.w, self.h, self.ss = w + 2 * pad, h + 2 * pad, ss
        self.img = Image.new("RGBA", (self.w * ss, self.h * ss), (0, 0, 0, 0))

    # coordinates are in final pixels (without padding); scaled here
    def s(self, v: float) -> int:
        return int(round((v + self.pad) * self.ss))

    def pts(self, points: Iterable[tuple[float, float]]) -> list[tuple[int, int]]:
        return [(self.s(x), self.s(y)) for x, y in points]

    def mask(
        self, draw: Callable[[ImageDraw.ImageDraw, "Canvas"], None]
    ) -> Image.Image:
        m = Image.new("L", self.img.size, 0)
        draw(ImageDraw.Draw(m), self)
        return m

    def fill(
        self,
        mask: Image.Image,
        top: Color,
        bottom: Color | None = None,
        outline: Color | None = None,
        outline_px: float = 2.0,
        alpha: float = 1.0,
    ) -> None:
        """Gradient fill through ``mask`` with an optional soft outline."""
        if outline is not None and outline_px > 0:
            size = max(3, int(outline_px * self.ss) * 2 + 1)
            ring = mask.filter(ImageFilter.MaxFilter(size))
            self.paint_solid(ring, outline, alpha)
        layer = gradient_layer(self.img.size, top, bottom or top)
        if alpha < 1:
            mask = mask.point(lambda v: int(v * alpha))
        self.img.paste(layer, (0, 0), mask)

    def paint_solid(self, mask: Image.Image, color: Color, alpha: float = 1.0) -> None:
        solid = Image.new("RGBA", self.img.size, rgba(color))
        if alpha < 1:
            mask = mask.point(lambda v: int(v * alpha))
        self.img.paste(solid, (0, 0), mask)

    def over(self, other: Image.Image, blur: float = 0) -> None:
        if blur:
            other = other.filter(ImageFilter.GaussianBlur(blur * self.ss))
        self.img = Image.alpha_composite(self.img, other)

    def glow(
        self, mask: Image.Image, color: Color, radius: float, strength: float = 0.8
    ) -> None:
        g = mask.filter(ImageFilter.GaussianBlur(radius * self.ss)).point(
            lambda v: int(min(255, v * strength))
        )
        layer = Image.new("RGBA", self.img.size, rgba(color))
        layer.putalpha(g)
        self.img = Image.alpha_composite(self.img, layer)

    def shadow(
        self, cx: float, cy: float, rx: float, ry: float, alpha: int = 90
    ) -> None:
        m = self.mask(
            lambda d, c: d.ellipse(
                [c.s(cx - rx), c.s(cy - ry), c.s(cx + rx), c.s(cy + ry)], fill=alpha
            )
        )
        m = m.filter(ImageFilter.GaussianBlur(ry * 0.6 * self.ss))
        layer = Image.new("RGBA", self.img.size, (10, 12, 30, 255))
        layer.putalpha(m)
        self.img = Image.alpha_composite(layer, self.img)

    def final(self) -> Image.Image:
        return self.img.resize((self.w, self.h), Image.LANCZOS)


def gradient_layer(size: tuple[int, int], top: Color, bottom: Color) -> Image.Image:
    w, h = size
    t = np.linspace(0.0, 1.0, h, dtype=np.float32)[:, None]
    top_a = np.array(rgba(top), dtype=np.float32)
    bot_a = np.array(rgba(bottom), dtype=np.float32)
    col = top_a[None, :] * (1 - t) + bot_a[None, :] * t
    arr = np.repeat(col[:, None, :], w, axis=1).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def radial_layer(size, center, radius, color: Color, power: float = 2.0) -> Image.Image:
    w, h = size
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    d = np.sqrt((xx - center[0]) ** 2 + (yy - center[1]) ** 2) / max(1.0, radius)
    a = np.clip(1.0 - d, 0.0, 1.0) ** power
    c = rgba(color)
    arr = np.zeros((h, w, 4), dtype=np.uint8)
    arr[..., 0], arr[..., 1], arr[..., 2] = c[0], c[1], c[2]
    arr[..., 3] = (a * c[3]).astype(np.uint8)
    return Image.fromarray(arr, "RGBA")


def blob_points(
    cx, cy, rx, ry, rng: random.Random, n=18, wobble=0.12
) -> list[tuple[float, float]]:
    out = []
    for i in range(n):
        a = 2 * math.pi * i / n
        r = 1 + rng.uniform(-wobble, wobble)
        out.append((cx + math.cos(a) * rx * r, cy + math.sin(a) * ry * r))
    return out


def ridge(
    width: int,
    base: float,
    amp: float,
    rng: random.Random,
    rough: float = 0.55,
    steps: int = 9,
    wrap=True,
) -> np.ndarray:
    """Midpoint-displacement ridge line (tileable when wrap)."""
    n = 2**steps + 1
    y = np.zeros(n)
    y[0] = y[-1] = base
    span, scale = n - 1, amp
    while span > 1:
        half = span // 2
        for i in range(half, n - 1, span):
            y[i] = (y[i - half] + y[i + half]) / 2 + rng.uniform(-scale, scale)
        span, scale = half, scale * rough
    if wrap:
        y[-1] = y[0]
    xs = np.linspace(0, n - 1, width)
    return np.interp(xs, np.arange(n), y)


def polygon_from_ridge(
    ys: Sequence[float], width: int, bottom: float
) -> list[tuple[float, float]]:
    pts = [(0.0, bottom)]
    step = max(1, len(ys) // 400)
    for i in range(0, len(ys), step):
        pts.append((i * width / (len(ys) - 1), float(ys[i])))
    pts.append((float(width), float(ys[-1])))
    pts.append((float(width), bottom))
    return pts


def soften(img: Image.Image, px: float) -> Image.Image:
    return img.filter(ImageFilter.GaussianBlur(px))


def multiply_alpha(img: Image.Image, f: float) -> Image.Image:
    r, g, b, a = img.split()
    a = a.point(lambda v: int(v * f))
    return Image.merge("RGBA", (r, g, b, a))


def lighten_top(mask: Image.Image, shift_px: int) -> Image.Image:
    """Highlight band: the part of the mask whose top edge is exposed."""
    moved = ImageChops.offset(mask, 0, shift_px)
    return ImageChops.subtract(mask, moved)
