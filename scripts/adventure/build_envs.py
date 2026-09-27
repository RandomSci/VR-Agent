"""Bake the layered adventure environments (original, procedural, deterministic).

    uv run --with pillow --with numpy python scripts/adventure/build_envs.py [kit ...]

Each kit gets five 1920x1080 layers in frontend/vr-agent/world/env/<kit>/:

    sky.jpg   opaque backdrop (gradient, stars, moon or sun, glow)
    far.png   distant silhouettes (mountains, skyline)       tileable, parallax 0.15
    mid.png   middle distance (trees, buildings, pillars)    tileable, parallax 0.4
    near.png  the ground the characters stand on             tileable, parallax 0.8
    fore.png  foreground framing at the edges (in front)      tileable, parallax 1.25

far/mid/near/fore wrap horizontally without a seam, so traversal can slide the
world past the characters (the characters never fake a long walk). Any file
can be replaced by hand-made art of the same size.
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).parent))
from art_kit import gradient_layer, radial_layer, ridge, shade  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "frontend" / "vr-agent" / "world" / "env"
W, H = 1920, 1080
SS = 2  # supersampling for these big layers


# ---------------------------------------------------------------------------
# helpers (all coordinates in final pixels)
# ---------------------------------------------------------------------------


class Layer:
    def __init__(self):
        self.img = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))

    def mask(self) -> tuple[Image.Image, ImageDraw.ImageDraw]:
        m = Image.new("L", self.img.size, 0)
        return m, ImageDraw.Draw(m)

    def fill(
        self,
        m: Image.Image,
        top,
        bottom=None,
        alpha: float = 1.0,
        y0: float = 0,
        y1: float = H,
    ):
        """Gradient between screen rows y0..y1 through mask m."""
        g = Image.new("RGBA", self.img.size, (0, 0, 0, 0))
        band = gradient_layer((W * SS, max(2, int((y1 - y0) * SS))), top, bottom or top)
        g.paste(band, (0, int(y0 * SS)))
        if y0 > 0:
            g.paste(
                Image.new("RGBA", (W * SS, int(y0 * SS)), tuple(list(top[:3]) + [255])),
                (0, 0),
            )
        if y1 < H:
            g.paste(
                Image.new(
                    "RGBA",
                    (W * SS, int((H - y1) * SS)),
                    tuple(list((bottom or top)[:3]) + [255]),
                ),
                (0, int(y1 * SS)),
            )
        if alpha < 1:
            m = m.point(lambda v: int(v * alpha))
        self.img.paste(g, (0, 0), m)

    def over(self, other: Image.Image):
        self.img = Image.alpha_composite(self.img, other)

    def blurred(self, m: Image.Image, px: float) -> Image.Image:
        return m.filter(ImageFilter.GaussianBlur(px * SS))

    def save(self, path: Path, opaque=False):
        img = self.img.resize((W, H), Image.LANCZOS)
        path.parent.mkdir(parents=True, exist_ok=True)
        if opaque:  # the sky: grain makes PNG huge, JPEG is ideal
            img.convert("RGB").save(
                path.with_suffix(".jpg"), quality=88, optimize=True, progressive=True
            )
            return img
        img.save(path, optimize=True)
        return img


def s(v: float) -> int:
    return int(round(v * SS))


def wrapped(x: float, span: float) -> list[float]:
    """Draw near the edges twice so the layer tiles horizontally."""
    xs = [x]
    if x - span < 0:
        xs.append(x + W)
    if x + span > W:
        xs.append(x - W)
    return xs


def poly(d: ImageDraw.ImageDraw, pts, fill=255):
    d.polygon([(s(x), s(y)) for x, y in pts], fill=fill)


def ellipse(d, cx, cy, rx, ry, fill=255):
    d.ellipse([s(cx - rx), s(cy - ry), s(cx + rx), s(cy + ry)], fill=fill)


def ridge_mask(base: float, amp: float, rng, rough=0.55, steps=9) -> Image.Image:
    ys = ridge(W * SS, base * SS, amp * SS, rng, rough, steps, wrap=True)
    m = Image.new("L", (W * SS, H * SS), 0)
    arr = np.array(m)
    rows = np.arange(H * SS)[:, None]
    arr[rows >= ys[None, :]] = 255
    return Image.fromarray(arr.astype(np.uint8), "L")


def grain(layer: Layer, amount=6, seed=1):
    """A little film grain so big gradients never band or look flat."""
    rng = np.random.default_rng(seed)
    arr = np.array(layer.img).astype(np.int16)
    noise = rng.integers(-amount, amount + 1, size=arr.shape[:2])
    for c in range(3):
        arr[..., c] = np.clip(arr[..., c] + noise, 0, 255)
    layer.img = Image.fromarray(arr.astype(np.uint8), "RGBA")


# ---------------------------------------------------------------------------
# sky
# ---------------------------------------------------------------------------


def sky(stops, stars=0, moon=None, sun=None, glow=None, nebula=None, seed=1) -> Layer:
    L = Layer()
    w, h = L.img.size
    ys = np.linspace(0, 1, h)
    cols = np.zeros((h, 3))
    pos = [p for p, _ in stops]
    for c in range(3):
        cols[:, c] = np.interp(ys, pos, [col[c] for _, col in stops])
    arr = np.repeat(cols[:, None, :], w, axis=1).astype(np.uint8)
    L.img = Image.fromarray(np.dstack([arr, np.full((h, w), 255, np.uint8)]), "RGBA")
    rng = random.Random(seed)
    if nebula:
        for cx, cy, r, col in nebula:
            L.over(
                radial_layer(L.img.size, (s(cx), s(cy)), s(r), (*col, 90), power=1.6)
            )
    if glow:
        cx, cy, r, col = glow
        L.over(radial_layer(L.img.size, (s(cx), s(cy)), s(r), (*col, 150), power=1.8))
    if stars:
        m, d = L.mask()
        for _ in range(stars):
            x, y = rng.uniform(0, W), rng.uniform(0, H * 0.62) ** 1.0
            r = rng.choice([0.6, 0.8, 0.8, 1.0, 1.2, 1.6])
            a = int(rng.uniform(120, 255) * (1 - y / (H * 0.75)))
            ellipse(d, x, y, r, r, max(40, a))
        L.fill(m, (255, 255, 255))
        big = Layer()
        m2, d2 = big.mask()
        for _ in range(stars // 25):
            x, y = rng.uniform(0, W), rng.uniform(0, H * 0.5)
            ellipse(d2, x, y, 2.2, 2.2)
        L.over(radial_like_glow(m2, (200, 220, 255), 5))
        L.fill(m2, (255, 255, 255))
    if moon:
        cx, cy, r = moon
        L.over(
            radial_layer(
                L.img.size, (s(cx), s(cy)), s(r * 7), (200, 220, 255, 110), power=2.2
            )
        )
        m, d = L.mask()
        ellipse(d, cx, cy, r, r)
        L.fill(m, (255, 252, 236), (228, 230, 240))
        m2, d2 = L.mask()
        for dx, dy, rr in ((-0.3, -0.2, 0.18), (0.25, 0.1, 0.12), (-0.05, 0.35, 0.1)):
            ellipse(d2, cx + dx * r, cy + dy * r, rr * r, rr * r)
        L.fill(m2, (214, 214, 228), alpha=0.6)
    if sun:
        cx, cy, r, col = sun
        L.over(
            radial_layer(L.img.size, (s(cx), s(cy)), s(r * 8), (*col, 170), power=2.0)
        )
        m, d = L.mask()
        ellipse(d, cx, cy, r, r)
        L.fill(m, (255, 246, 214))
    grain(L, 4, seed)
    return L


def radial_like_glow(mask: Image.Image, color, px) -> Image.Image:
    g = mask.filter(ImageFilter.GaussianBlur(px * SS))
    layer = Image.new("RGBA", mask.size, (*color, 255))
    layer.putalpha(g)
    return layer


# ---------------------------------------------------------------------------
# silhouettes
# ---------------------------------------------------------------------------


def mountains(L: Layer, base, amp, top, bottom, rng, rough=0.55, snow=None, haze=None):
    ys = ridge(W * SS, base * SS, amp * SS, rng, rough, 9, wrap=True)
    rows = np.arange(H * SS)[:, None]
    body = (rows >= ys[None, :]).astype(np.uint8) * 255
    m = Image.fromarray(body, "L")
    L.fill(m, top, bottom, y0=base - amp, y1=H)
    # sun side: a lighter face on slopes that fall to the right
    slope = np.gradient(ys)
    lit = np.clip(-slope / (np.abs(slope).max() + 1e-6), 0, 1)
    depth = (rows - ys[None, :]) / (amp * SS * 1.2)
    face = (np.clip(1 - depth, 0, 1) * lit[None, :] * (body > 0) * 120).astype(np.uint8)
    L.fill(
        Image.fromarray(face, "L").filter(ImageFilter.GaussianBlur(4 * SS)),
        shade(top, 1.25),
    )
    if snow:
        # caps only on the peaks: above a snow line, reaching down a varying depth
        line = (base + amp * 0.15) * SS
        noise = ridge(W * SS, 0, 1, rng, 0.7, 8, wrap=True)
        reach = (snow[1] + 28 * (noise - noise.min()) / (np.ptp(noise) + 1e-6)) * SS
        cap = (
            (body > 0)
            & (ys[None, :] < line)
            & (
                rows
                < ys[None, :]
                + reach[None, :]
                * (1 - (ys[None, :] - (base - amp) * SS) / (amp * SS * 1.2))
            )
        )
        cm = Image.fromarray((cap * 255).astype(np.uint8), "L").filter(
            ImageFilter.GaussianBlur(1.2 * SS)
        )
        L.fill(cm, snow[0], shade(snow[0], 0.9), alpha=0.92)
    if haze:
        arr = np.zeros(body.shape, np.float32)
        rowsf = np.linspace(0, 1, body.shape[0])[:, None]
        centre = (base + amp * 0.2) / H
        arr[:] = np.clip(1 - np.abs(rowsf - centre) / 0.12, 0, 1) * 255
        hz = Image.fromarray(arr.astype(np.uint8), "L")
        L.fill(ImageChops.multiply(hz, m), haze, alpha=0.5)
    return m


def pine(d, x, base, h, w, rng):
    tiers = rng.randint(4, 6)
    for t in range(tiers):
        k = t / tiers
        ty = base - h * (0.18 + 0.82 * k)
        tw = w * (1 - k * 0.8)
        by = base - h * 0.15 - h * 0.8 * k + h * 0.18
        poly(d, [(x - tw, by), (x + tw, by), (x + tw * 0.08, ty), (x - tw * 0.08, ty)])
    d.rectangle(
        [s(x - w * 0.08), s(base - h * 0.2), s(x + w * 0.08), s(base + 4)], fill=255
    )


def roundtree(d, x, base, h, w, rng):
    d.rectangle(
        [s(x - w * 0.07), s(base - h * 0.45), s(x + w * 0.07), s(base + 4)], fill=255
    )
    for _ in range(7):
        cx = x + rng.uniform(-w * 0.45, w * 0.45)
        cy = base - h * rng.uniform(0.55, 0.9)
        r = w * rng.uniform(0.3, 0.5)
        ellipse(d, cx, cy, r, r * 0.9)


def treeline(
    L, kind, base, count, hmin, hmax, top, bottom, rng, alpha=1.0, fill_below=True
):
    m, d = L.mask()
    for _ in range(count):
        x = rng.uniform(0, W)
        h = rng.uniform(hmin, hmax)
        w = h * (0.22 if kind == "pine" else 0.45)
        b = base + rng.uniform(-10, 14)
        for xx in wrapped(x, w):
            (pine if kind == "pine" else roundtree)(d, xx, b, h, w, rng)
    if fill_below:
        d.rectangle([0, s(base), s(W), s(H)], fill=255)
    L.fill(m, top, bottom, alpha, y0=base - hmax, y1=H)
    return m


def buildings(
    L,
    base,
    count,
    hmin,
    hmax,
    wmin,
    wmax,
    top,
    bottom,
    window,
    rng,
    lit=0.35,
    antenna=0.2,
):
    m, d = L.mask()
    rects = []
    x = 0.0
    while x < W:
        w = rng.uniform(wmin, wmax)
        h = rng.uniform(hmin, hmax)
        rects.append((x, base - h, x + w, base))
        x += w + rng.uniform(-8, 6)
    for x0, y0, x1, y1 in rects:
        d.rectangle([s(x0), s(y0), s(x1), s(H)], fill=255)
        if rng.random() < antenna:
            cx = (x0 + x1) / 2
            d.rectangle(
                [s(cx - 1.5), s(y0 - rng.uniform(20, 60)), s(cx + 1.5), s(y0)], fill=255
            )
        if rng.random() < 0.25:  # water tank
            cx = x0 + (x1 - x0) * rng.uniform(0.3, 0.7)
            d.rectangle([s(cx - 12), s(y0 - 22), s(cx + 12), s(y0)], fill=255)
    L.fill(m, top, bottom, y0=base - hmax, y1=H)
    wm, wd = L.mask()
    for x0, y0, x1, y1 in rects:
        cols = max(1, int((x1 - x0 - 8) // 11))
        rows = max(1, int((y1 - y0 - 10) // 15))
        for r in range(rows):
            for c in range(cols):
                if rng.random() < lit:
                    wx, wy = x0 + 6 + c * 11, y0 + 8 + r * 15
                    wd.rectangle(
                        [s(wx), s(wy), s(wx + 5), s(wy + 7)],
                        fill=int(rng.uniform(150, 255)),
                    )
    wm = ImageChops.multiply(wm, m)
    L.over(radial_like_glow(wm, window, 3))
    L.fill(wm, window)
    return m


def ground(
    L,
    top_y,
    top,
    bottom,
    path=None,
    rng=None,
    tufts=0,
    tuft_col=None,
    stones=0,
    stone_col=None,
):
    m, d = L.mask()
    ys = ridge(W * SS, top_y * SS, 10 * SS, rng or random.Random(1), 0.5, 8, wrap=True)
    arr = np.zeros((H * SS, W * SS), np.uint8)
    rows = np.arange(H * SS)[:, None]
    arr[rows >= ys[None, :]] = 255
    m = Image.fromarray(arr, "L")
    L.fill(m, top, bottom, y0=top_y, y1=H)
    if path:
        pm, pd = L.mask()
        # a soft lighter band the characters stand on, across the whole width
        pd.rectangle([0, s(top_y + 40), s(W), s(H)], fill=255)
        band = pm.filter(ImageFilter.GaussianBlur(26 * SS))
        L.fill(
            ImageChops.multiply(band, m), path[0], path[1], alpha=0.55, y0=top_y, y1=H
        )
    rng = rng or random.Random(2)
    if stones:
        sm, sd = L.mask()
        for _ in range(stones):
            x, y = rng.uniform(0, W), rng.uniform(top_y + 30, H - 8)
            rx = rng.uniform(4, 14) * (0.5 + (y - top_y) / (H - top_y))
            for xx in wrapped(x, rx):
                ellipse(sd, xx, y, rx, rx * 0.45)
        L.fill(sm, stone_col or shade(top, 1.3), alpha=0.7)
    if tufts:
        tm, td = L.mask()
        for _ in range(tufts):
            x = rng.uniform(0, W)
            y = rng.uniform(top_y - 4, H)
            h = rng.uniform(6, 22) * (0.4 + (y - top_y) / (H - top_y))
            for xx in wrapped(x, 12):
                for k in range(5):
                    dx = (k - 2) * 2.4
                    poly(
                        td,
                        [
                            (xx + dx - 1.4, y),
                            (xx + dx + 1.4, y),
                            (xx + dx * 2.2, y - h * rng.uniform(0.7, 1.1)),
                        ],
                    )
        L.fill(tm, tuft_col or shade(top, 1.25), alpha=0.85)
    return m


def fog_band(L, y, height, color, alpha=0.5, seed=3):
    rng = np.random.default_rng(seed)
    w, h = L.img.size
    rows = np.linspace(0, H, h)[:, None]
    base = np.clip(1 - np.abs(rows - y) / height, 0, 1) ** 1.5
    # soft wisps that tile horizontally
    xs = np.linspace(0, 2 * np.pi, w)[None, :]
    wisp = 0.75 + 0.25 * (
        np.sin(xs * 3 + rng.uniform(0, 6)) * np.sin(xs * 7 + rng.uniform(0, 6))
    )
    a = (base * wisp * 255 * alpha).astype(np.uint8)
    layer = Image.new("RGBA", (w, h), (*color, 255))
    layer.putalpha(Image.fromarray(a, "L"))
    L.over(layer)


def fern(d, x, y, size, rng, side=1):
    for k in range(7):
        a = math.radians(-100 + k * 22 + rng.uniform(-6, 6))
        length = size * rng.uniform(0.7, 1.1)
        ex, ey = x + math.cos(a) * length, y + math.sin(a) * length
        mx, my = (
            x + math.cos(a) * length * 0.5,
            y + math.sin(a) * length * 0.5 - size * 0.08,
        )
        poly(
            d,
            [
                (x - 2, y),
                (mx - size * 0.06, my),
                (ex, ey),
                (mx + size * 0.06, my),
                (x + 2, y),
            ],
        )


def foreground_edges(L, color, rng, kind="plants", height=260, bottom=H):
    """Framing silhouettes only near the left and right edges (never centre)."""
    m, d = L.mask()
    for side in (0, 1):
        for _ in range(9):
            x = rng.uniform(-60, 230) if side == 0 else rng.uniform(W - 230, W + 60)
            if kind == "plants":
                fern(d, x, bottom + 10, rng.uniform(height * 0.5, height), rng)
            elif kind == "rocks":
                ellipse(
                    d,
                    x,
                    bottom - rng.uniform(0, 40),
                    rng.uniform(40, 110),
                    rng.uniform(30, 70),
                )
            elif kind == "grass":
                for k in range(14):
                    xx = x + rng.uniform(-40, 40)
                    h = rng.uniform(height * 0.4, height)
                    poly(
                        d,
                        [
                            (xx - 3, bottom),
                            (xx + 3, bottom),
                            (xx + rng.uniform(-30, 30), bottom - h),
                        ],
                    )
            elif kind == "reeds":
                for k in range(8):
                    xx = x + rng.uniform(-50, 50)
                    h = rng.uniform(height * 0.5, height)
                    tip = xx + rng.uniform(-14, 14)
                    poly(
                        d,
                        [
                            (xx - 2, bottom),
                            (xx + 2, bottom),
                            (tip + 1, bottom - h),
                            (tip - 1, bottom - h),
                        ],
                    )
                    ellipse(d, tip, bottom - h * 0.92, 4, 14)
    if kind == "trunks":
        for x, w_ in (
            (rng.uniform(-30, 40), rng.uniform(80, 110)),
            (rng.uniform(W - 40, W + 20), rng.uniform(90, 120)),
        ):
            bend = rng.uniform(-18, 18)
            pts = [(x - w_ / 2 + bend, -10), (x + w_ / 2 + bend, -10)]
            pts += [(x + w_ * 0.55 + bend * (1 - t), t * H) for t in (0.3, 0.6, 0.85)]
            pts += [(x + w_ * 1.1, H + 10), (x - w_ * 1.1, H + 10)]  # root flare
            pts += [(x - w_ * 0.55 + bend * (1 - t), t * H) for t in (0.85, 0.6, 0.3)]
            poly(d, pts)
        for cx in (
            rng.uniform(40, 200),
            rng.uniform(W - 200, W - 40),
        ):  # leaf canopy at the top corners
            for _ in range(10):
                ellipse(
                    d,
                    cx + rng.uniform(-200, 200),
                    rng.uniform(-60, 90),
                    rng.uniform(60, 120),
                    rng.uniform(40, 80),
                )
    L.fill(m, color)


def column(d, x, base, h, w, broken=False, rng=None):
    top = base - h
    d.rectangle([s(x - w / 2), s(top), s(x + w / 2), s(base)], fill=255)
    d.rectangle([s(x - w * 0.7), s(base - 16), s(x + w * 0.7), s(base)], fill=255)
    if broken and rng:
        poly(
            d,
            [
                (x - w / 2, top),
                (x + w / 2, top),
                (x + w / 2, top - rng.uniform(10, 40)),
                (x, top - rng.uniform(0, 20)),
                (x - w / 2, top - rng.uniform(5, 30)),
            ],
        )
    else:
        d.rectangle([s(x - w * 0.75), s(top - 14), s(x + w * 0.75), s(top)], fill=255)


def arch(d, x, base, h, w, thick):
    d.rectangle([s(x - w / 2 - thick), s(base - h), s(x - w / 2), s(base)], fill=255)
    d.rectangle([s(x + w / 2), s(base - h), s(x + w / 2 + thick), s(base)], fill=255)
    d.pieslice(
        [
            s(x - w / 2 - thick),
            s(base - h - w / 2 - thick),
            s(x + w / 2 + thick),
            s(base - h + w / 2 + thick),
        ],
        180,
        360,
        fill=255,
    )
    d.pieslice(
        [s(x - w / 2), s(base - h - w / 2), s(x + w / 2), s(base - h + w / 2)],
        180,
        360,
        fill=0,
    )


# ---------------------------------------------------------------------------
# kits
# ---------------------------------------------------------------------------


def kit_city():
    rng = random.Random(101)
    sk = sky(
        [
            (0, (16, 18, 52)),
            (0.55, (52, 40, 104)),
            (0.82, (150, 84, 128)),
            (1, (210, 120, 120)),
        ],
        stars=260,
        moon=(1480, 170, 46),
        seed=101,
    )
    far = Layer()
    buildings(
        far,
        720,
        0,
        120,
        330,
        40,
        110,
        (58, 56, 112),
        (74, 64, 128),
        (160, 180, 255),
        rng,
        0.18,
        0.3,
    )
    fog_band(far, 700, 90, (160, 120, 170), 0.35)
    mid = Layer()
    buildings(
        mid,
        800,
        0,
        160,
        420,
        70,
        170,
        (30, 28, 66),
        (40, 34, 76),
        (255, 206, 130),
        rng,
        0.3,
        0.25,
    )
    near = Layer()
    ground(
        near,
        860,
        (44, 40, 70),
        (26, 24, 44),
        path=((88, 80, 120), (60, 52, 90)),
        rng=rng,
        stones=0,
    )
    rm, rd = near.mask()  # rooftop railing
    for x in range(0, W, 48):
        rd.rectangle([s(x), s(800), s(x + 4), s(862)], fill=255)
    rd.rectangle([0, s(796), s(W), s(804)], fill=255)
    near.fill(rm, (20, 18, 40))
    fore = Layer()
    foreground_edges(fore, (14, 18, 30), rng, "plants", 300)
    return sk, far, mid, near, fore


def kit_outskirts():
    rng = random.Random(202)
    sk = sky(
        [
            (0, (60, 70, 140)),
            (0.45, (190, 120, 150)),
            (0.75, (250, 170, 120)),
            (1, (255, 214, 150)),
        ],
        stars=40,
        sun=(560, 640, 44, (255, 200, 120)),
        seed=202,
    )
    far = Layer()
    mountains(
        far, 640, 70, (120, 110, 170), (150, 130, 180), rng, 0.5, haze=(255, 200, 170)
    )
    mid = Layer()
    treeline(mid, "round", 760, 26, 90, 190, (70, 64, 110), (60, 54, 96), rng)
    fm, fd = mid.mask()  # a wooden fence
    for x in range(0, W, 70):
        fd.rectangle([s(x), s(770), s(x + 7), s(820)], fill=255)
    fd.rectangle([0, s(784), s(W), s(790)], fill=255)
    fd.rectangle([0, s(804), s(W), s(810)], fill=255)
    mid.fill(fm, (60, 44, 60))
    near = Layer()
    ground(
        near,
        850,
        (120, 110, 90),
        (70, 62, 60),
        path=((200, 170, 130), (150, 120, 100)),
        rng=rng,
        tufts=500,
        tuft_col=(140, 150, 100),
        stones=60,
    )
    fore = Layer()
    foreground_edges(fore, (40, 34, 50), rng, "grass", 280)
    return sk, far, mid, near, fore


def kit_forest(deep=False):
    rng = random.Random(303 if not deep else 404)
    if deep:
        sk = sky(
            [(0, (6, 14, 30)), (0.6, (14, 40, 60)), (1, (30, 70, 80))],
            stars=120,
            moon=(420, 150, 34),
            seed=404,
        )
    else:
        sk = sky(
            [(0, (10, 18, 44)), (0.6, (28, 50, 90)), (1, (60, 100, 120))],
            stars=200,
            moon=(1380, 160, 40),
            seed=303,
        )
    far = Layer()
    treeline(
        far,
        "pine",
        700,
        70,
        180,
        330,
        (40, 70, 100) if not deep else (20, 50, 64),
        (50, 84, 110) if not deep else (26, 60, 70),
        rng,
    )
    fog_band(far, 690, 70, (120, 160, 180) if not deep else (80, 150, 150), 0.45)
    mid = Layer()
    treeline(
        mid,
        "pine",
        790,
        40,
        260,
        520,
        (18, 36, 52) if not deep else (10, 26, 34),
        (22, 42, 56) if not deep else (12, 30, 38),
        rng,
    )
    if deep:
        gm, gd = mid.mask()  # glowing moss dots on the trunks
        for _ in range(160):
            ellipse(
                gd,
                rng.uniform(0, W),
                rng.uniform(560, 800),
                rng.uniform(1.5, 3.5),
                rng.uniform(1.5, 3.5),
            )
        mid.over(radial_like_glow(gm, (120, 255, 220), 5))
        mid.fill(gm, (190, 255, 230))
    near = Layer()
    ground(
        near,
        850,
        (34, 56, 60) if not deep else (20, 40, 44),
        (18, 30, 36),
        path=((80, 104, 96), (50, 70, 66)),
        rng=rng,
        tufts=420,
        tuft_col=(60, 96, 80),
        stones=40,
        stone_col=(80, 96, 100),
    )
    if deep:  # a quiet pond on the right
        pm, pd = near.mask()
        ellipse(pd, 1560, 930, 330, 56)
        near.fill(pm, (40, 80, 110), (20, 50, 80))
        hm, hd = near.mask()
        for k in range(9):
            y = 900 + k * 7
            hd.rectangle(
                [s(1330 + k * 18), s(y), s(1790 - k * 20), s(y + 1.6)], fill=150
            )
        near.fill(ImageChops.multiply(hm, pm), (170, 230, 255), alpha=0.6)
    fore = Layer()
    foreground_edges(fore, (8, 16, 22), rng, "trunks")
    foreground_edges(fore, (10, 22, 26), rng, "plants", 240)
    return sk, far, mid, near, fore


def kit_river():
    rng = random.Random(505)
    sk = sky(
        [
            (0, (120, 150, 200)),
            (0.5, (220, 180, 200)),
            (0.8, (255, 210, 190)),
            (1, (255, 236, 210)),
        ],
        stars=0,
        sun=(1500, 560, 38, (255, 220, 170)),
        seed=505,
    )
    far = Layer()
    mountains(
        far,
        600,
        150,
        (130, 140, 190),
        (160, 160, 200),
        rng,
        0.55,
        snow=((250, 240, 250), 18),
        haze=(255, 220, 220),
    )
    mid = Layer()
    treeline(mid, "round", 780, 30, 110, 230, (70, 100, 110), (60, 86, 96), rng)
    near = Layer()
    ground(
        near,
        820,
        (90, 120, 100),
        (60, 84, 76),
        path=((150, 170, 130), (110, 130, 100)),
        rng=rng,
        tufts=300,
        tuft_col=(110, 150, 110),
        stones=0,
    )
    wm, wd = near.mask()  # the river band behind the path
    wd.rectangle([0, s(832), s(W), s(890)], fill=255)
    near.fill(wm, (150, 190, 230), (90, 140, 190), y0=832, y1=890)
    sm, sd = near.mask()
    for _ in range(120):
        x, y = rng.uniform(0, W), rng.uniform(836, 886)
        sd.rectangle([s(x), s(y), s(x + rng.uniform(10, 40)), s(y + 1.4)], fill=200)
    near.fill(sm, (255, 255, 255), alpha=0.7)
    bm, bd = near.mask()
    for _ in range(40):
        x = rng.uniform(0, W)
        for xx in wrapped(x, 20):
            ellipse(
                bd, xx, rng.uniform(884, 900), rng.uniform(8, 20), rng.uniform(4, 8)
            )
    near.fill(bm, (120, 124, 130), (90, 94, 104))
    fore = Layer()
    foreground_edges(fore, (40, 60, 56), rng, "reeds", 320)
    return sk, far, mid, near, fore


def kit_mountain():
    rng = random.Random(606)
    sk = sky(
        [(0, (130, 150, 180)), (0.6, (190, 200, 214)), (1, (220, 224, 230))],
        stars=0,
        seed=606,
    )
    far = Layer()
    mountains(
        far,
        560,
        260,
        (128, 140, 172),
        (160, 170, 196),
        rng,
        0.58,
        snow=((248, 250, 255), 46),
        haze=(230, 234, 240),
    )
    fog_band(far, 690, 90, (230, 234, 240), 0.35)
    mid = Layer()
    mountains(mid, 700, 140, (104, 110, 134), (120, 124, 146), rng, 0.62)
    fog_band(mid, 760, 80, (220, 226, 234), 0.45, 5)
    near = Layer()
    ground(
        near,
        840,
        (110, 108, 118),
        (78, 76, 90),
        path=((160, 150, 150), (120, 112, 116)),
        rng=rng,
        stones=160,
        stone_col=(140, 138, 150),
        tufts=120,
        tuft_col=(120, 130, 110),
    )
    fore = Layer()
    foreground_edges(fore, (70, 70, 84), rng, "rocks")
    return sk, far, mid, near, fore


def kit_valley():
    rng = random.Random(707)
    sk = sky(
        [
            (0, (50, 40, 100)),
            (0.45, (150, 80, 130)),
            (0.75, (240, 140, 110)),
            (1, (255, 190, 130)),
        ],
        stars=60,
        sun=(1320, 640, 50, (255, 170, 110)),
        seed=707,
    )
    far = Layer()
    m, d = far.mask()
    lm, ld = far.mask()  # sunlit faces
    sm, sd = far.mask()  # strata lines
    mesas = sorted(
        (
            (rng.uniform(0, W), rng.uniform(90, 230), rng.uniform(90, 220))
            for _ in range(7)
        ),
        key=lambda v: -v[2],
    )
    for x, w_, h in mesas:
        for xx in wrapped(x, w_ * 1.3):
            top = 720 - h
            jag = [
                (xx - w_ * 0.85 + k * w_ * 1.7 / 6, top + rng.uniform(-6, 6))
                for k in range(7)
            ]
            body = (
                [(xx - w_ * 1.25, 720)]
                + [(xx - w_ * 0.95, top + h * 0.35), (xx - w_ * 0.85, top)]
                + jag
                + [
                    (xx + w_ * 0.85, top),
                    (xx + w_ * 0.95, top + h * 0.3),
                    (xx + w_ * 1.25, 720),
                ]
            )
            poly(d, body)
            poly(
                ld,
                [
                    (xx - w_ * 1.25, 720),
                    (xx - w_ * 0.95, top + h * 0.35),
                    (xx - w_ * 0.85, top),
                    (xx - w_ * 0.55, top),
                    (xx - w_ * 0.7, 720),
                ],
            )
            for k in range(1, 4):
                yy = top + h * k / 4.5
                sd.line(
                    [
                        s(xx - w_ * 0.9),
                        s(yy),
                        s(xx + w_ * 0.9),
                        s(yy + rng.uniform(-4, 4)),
                    ],
                    fill=160,
                    width=s(2),
                )
    d.rectangle([0, s(718), s(W), s(H)], fill=255)
    far.fill(m, (132, 72, 110), (156, 86, 112), y0=500, y1=H)
    far.fill(
        ImageChops.multiply(lm, m).filter(ImageFilter.GaussianBlur(2 * SS)),
        (200, 110, 110),
        alpha=0.7,
    )
    far.fill(ImageChops.multiply(sm, m), (100, 54, 90), alpha=0.6)
    fog_band(far, 720, 70, (255, 170, 140), 0.4)
    mid = Layer()
    m, d = mid.mask()
    for i in range(6):
        x = rng.uniform(0, W)
        for xx in wrapped(x, 60):
            column(
                d,
                xx,
                810,
                rng.uniform(120, 260),
                rng.uniform(22, 34),
                broken=rng.random() < 0.6,
                rng=rng,
            )
    d.rectangle([0, s(808), s(W), s(H)], fill=255)
    mid.fill(m, (80, 50, 80), (70, 44, 70), y0=540, y1=H)
    near = Layer()
    ground(
        near,
        860,
        (150, 110, 90),
        (100, 70, 70),
        path=((220, 170, 130), (170, 120, 100)),
        rng=rng,
        tufts=260,
        tuft_col=(190, 150, 90),
        stones=80,
        stone_col=(170, 130, 110),
    )
    fore = Layer()
    foreground_edges(fore, (60, 34, 50), rng, "grass", 240)
    return sk, far, mid, near, fore


def kit_ruins():
    rng = random.Random(808)
    sk = sky(
        [(0, (8, 6, 30)), (0.6, (30, 20, 70)), (1, (70, 40, 110))],
        stars=520,
        moon=(1500, 220, 70),
        nebula=((600, 260, 520, (120, 80, 220)), (1300, 380, 420, (60, 120, 220))),
        seed=808,
    )
    far = Layer()
    m, d = far.mask()
    gm, gd = far.mask()  # glowing grass rims
    for _ in range(5):  # floating islands: a flat grassy top, a craggy rock root
        x, y, w_ = rng.uniform(0, W), rng.uniform(250, 520), rng.uniform(70, 160)
        for xx in wrapped(x, w_ * 1.2):
            pts = [(xx - w_, y)]
            n = 9
            for k in range(1, n):
                t = k / n
                px = xx - w_ + 2 * w_ * t
                depth = math.sin(math.pi * t) ** 1.4 * w_ * rng.uniform(0.9, 1.4)
                pts.append((px + rng.uniform(-8, 8), y + depth))
            pts.append((xx + w_, y))
            pts += [(xx + w_ * 0.8, y - 10), (xx, y - 16), (xx - w_ * 0.8, y - 10)]
            poly(d, pts)
            ellipse(gd, xx, y - 10, w_ * 0.95, 9)
            if rng.random() < 0.7:  # a little tree
                roundtree(
                    d,
                    xx + rng.uniform(-w_ * 0.5, w_ * 0.5),
                    y - 12,
                    w_ * 0.7,
                    w_ * 0.5,
                    rng,
                )
    far.fill(m, (54, 42, 104), (36, 28, 76), y0=200, y1=700)
    far.over(radial_like_glow(gm, (120, 200, 255), 8))
    far.fill(gm, (110, 150, 220), alpha=0.8)
    mountains(far, 760, 60, (40, 30, 84), (50, 36, 90), rng, 0.5)
    mid = Layer()
    m, d = mid.mask()
    for i in range(4):
        x = (i + 0.5) * W / 4 + rng.uniform(-80, 80)
        if i % 2:
            for xx in wrapped(x, 140):
                arch(d, xx, 820, 240, 170, 38)
        else:
            for xx in wrapped(x, 40):
                column(d, xx, 820, rng.uniform(200, 320), 36, broken=True, rng=rng)
    d.rectangle([0, s(818), s(W), s(H)], fill=255)
    mid.fill(m, (34, 26, 70), (28, 22, 60), y0=480, y1=H)
    rm, rd = mid.mask()  # glowing runes on the stones
    for _ in range(40):
        x, y = rng.uniform(0, W), rng.uniform(600, 800)
        rd.line(
            [s(x), s(y), s(x + 8), s(y - 10), s(x + 16), s(y)], fill=255, width=s(2)
        )
    rm = ImageChops.multiply(rm, m)
    mid.over(radial_like_glow(rm, (140, 200, 255), 6))
    mid.fill(rm, (200, 236, 255))
    near = Layer()
    ground(
        near,
        860,
        (60, 50, 96),
        (36, 30, 64),
        path=((110, 100, 150), (80, 70, 120)),
        rng=rng,
        stones=0,
    )
    tm, td = near.mask()  # floor tiles
    for x in range(0, W, 120):
        td.line([s(x), s(870), s(x - 60), s(H)], fill=200, width=s(1.5))
    for y in (900, 950, 1010):
        td.line([0, s(y), s(W), s(y)], fill=200, width=s(1.5))
    near.fill(tm, (40, 34, 70), alpha=0.8)
    fore = Layer()
    foreground_edges(fore, (18, 14, 40), rng, "rocks")
    return sk, far, mid, near, fore


KITS = {
    "city": kit_city,
    "outskirts": kit_outskirts,
    "forest": lambda: kit_forest(False),
    "deep_forest": lambda: kit_forest(True),
    "river": kit_river,
    "mountain": kit_mountain,
    "valley": kit_valley,
    "ruins": kit_ruins,
}


def main(only: list[str] | None = None) -> None:
    for name, build in KITS.items():
        if only and name not in only:
            continue
        layers = build()
        folder = OUT / name
        sizes = []
        for layer_name, layer in zip(("sky", "far", "mid", "near", "fore"), layers):
            path = folder / (
                f"{layer_name}.jpg" if layer_name == "sky" else f"{layer_name}.png"
            )
            layer.save(path, opaque=layer_name == "sky")
            sizes.append(f"{layer_name} {path.stat().st_size // 1024}KB")
        print(name, ", ".join(sizes))


if __name__ == "__main__":
    main(sys.argv[1:] or None)
