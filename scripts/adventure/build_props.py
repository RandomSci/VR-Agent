"""Bake the world object sprites (frog, rabbit, campfire, signpost ...).

    uv run --with pillow --with numpy python scripts/adventure/build_props.py

Deterministic: the same code always makes the same files. Output goes to
frontend/vr-agent/world/props/<type>.png (+ <type>_fx.png for the parts the
renderer animates separately, e.g. the campfire flame). All art is original.
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

from PIL import ImageChops, ImageFilter

sys.path.insert(0, str(Path(__file__).parent))
from art_kit import Canvas, blob_points, radial_layer, shade  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "frontend" / "vr-agent" / "world" / "props"

INK = (28, 22, 48)


def ellipse(cx, cy, rx, ry):
    return lambda d, c: d.ellipse(
        [c.s(cx - rx), c.s(cy - ry), c.s(cx + rx), c.s(cy + ry)], fill=255
    )


def poly(points):
    return lambda d, c: d.polygon(c.pts(points), fill=255)


def eye(cv: Canvas, cx, cy, r, look=(0.15, 0.1)):
    cv.fill(
        cv.mask(ellipse(cx, cy, r, r * 1.08)),
        (255, 255, 255),
        (228, 232, 245),
        INK,
        1.2,
    )
    px, py = cx + look[0] * r, cy + look[1] * r
    cv.fill(cv.mask(ellipse(px, py, r * 0.62, r * 0.7)), (40, 30, 70), (20, 16, 40))
    cv.fill(
        cv.mask(ellipse(px - r * 0.22, py - r * 0.28, r * 0.22, r * 0.22)),
        (255, 255, 255),
    )
    cv.fill(
        cv.mask(ellipse(px + r * 0.2, py + r * 0.25, r * 0.1, r * 0.1)), (255, 255, 255)
    )


def blush(cv: Canvas, cx, cy, r):
    m = cv.mask(ellipse(cx, cy, r, r * 0.55)).filter(
        ImageFilter.GaussianBlur(r * 0.35 * cv.ss)
    )
    cv.paint_solid(m, (255, 120, 150), 0.55)


# ---------------------------------------------------------------------------


def frog():
    cv = Canvas(180, 150)
    cv.shadow(90, 138, 62, 10)
    green, dark = (118, 196, 92), (58, 128, 62)
    cv.fill(cv.mask(ellipse(90, 100, 62, 40)), shade(green, 1.15), dark, INK, 2.2)
    cv.fill(cv.mask(ellipse(90, 112, 40, 24)), (236, 244, 196), (206, 222, 158))
    for x in (60, 120):  # eye bumps
        cv.fill(cv.mask(ellipse(x, 62, 24, 22)), shade(green, 1.2), green, INK, 2.2)
        eye(cv, x, 60, 14)
    for x in (48, 132):  # front feet
        cv.fill(cv.mask(ellipse(x, 132, 18, 8)), green, dark, INK, 1.6)
    blush(cv, 58, 96, 11)
    blush(cv, 122, 96, 11)
    cv.fill(
        cv.mask(
            lambda d, c: d.arc(
                [c.s(78), c.s(92), c.s(102), c.s(106)],
                20,
                160,
                fill=255,
                width=c.s(2.2),
            )
        ),
        INK,
    )
    cv.fill(cv.mask(ellipse(70, 80, 16, 6)), (255, 255, 255), alpha=0.35)
    return cv.final()


def rabbit():
    cv = Canvas(150, 180)
    cv.shadow(75, 170, 48, 8)
    fur, fur2 = (252, 248, 244), (220, 214, 222)
    for x, tilt in ((56, -8), (92, 10)):  # ears
        pts = [(x - 11, 70), (x - 8 + tilt, 12), (x + 4 + tilt, 8), (x + 12, 70)]
        cv.fill(cv.mask(poly(pts)), fur, fur2, INK, 2)
        inner = [(x - 5, 64), (x - 3 + tilt, 22), (x + 3 + tilt, 20), (x + 6, 64)]
        cv.fill(cv.mask(poly(inner)), (255, 190, 206), (246, 160, 186))
    cv.fill(cv.mask(ellipse(80, 132, 50, 38)), fur, fur2, INK, 2.2)
    cv.fill(cv.mask(ellipse(122, 140, 14, 13)), (255, 255, 255), fur2, INK, 1.6)
    cv.fill(cv.mask(ellipse(70, 92, 36, 32)), fur, fur2, INK, 2.2)
    eye(cv, 62, 88, 9, (0.2, 0.15))
    eye(cv, 86, 88, 9, (0.2, 0.15))
    blush(cv, 52, 104, 8)
    blush(cv, 94, 104, 8)
    cv.fill(cv.mask(ellipse(74, 101, 4, 3)), (240, 130, 160))
    return cv.final()


def butterfly():
    cv = Canvas(120, 100, pad=22)
    wing = (130, 230, 255)
    m = cv.mask(
        lambda d, c: [
            d.ellipse([c.s(8), c.s(10), c.s(58), c.s(56)], fill=255),
            d.ellipse([c.s(62), c.s(10), c.s(112), c.s(56)], fill=255),
            d.ellipse([c.s(20), c.s(48), c.s(56), c.s(88)], fill=255),
            d.ellipse([c.s(64), c.s(48), c.s(100), c.s(88)], fill=255),
        ]
    )
    cv.glow(m, (120, 220, 255), 10, 0.9)
    cv.fill(m, shade(wing, 1.3), (160, 120, 255), (60, 70, 150), 1.4)
    cv.fill(cv.mask(ellipse(60, 50, 5, 30)), (60, 50, 90), outline=INK, outline_px=1)
    return cv.final()


def owl():
    cv = Canvas(150, 190)
    brown, dark = (158, 116, 84), (96, 66, 50)
    cv.fill(
        cv.mask(poly([(0, 172), (150, 164), (150, 178), (0, 186)])),
        (110, 80, 60),
        (70, 50, 40),
        INK,
        1.8,
    )
    cv.fill(cv.mask(poly([(40, 40), (46, 12), (62, 34)])), brown, dark, INK, 1.8)
    cv.fill(cv.mask(poly([(110, 40), (104, 12), (88, 34)])), brown, dark, INK, 1.8)
    cv.fill(cv.mask(ellipse(75, 108, 52, 64)), shade(brown, 1.1), dark, INK, 2.2)
    cv.fill(cv.mask(ellipse(75, 122, 34, 44)), (236, 214, 180), (206, 176, 140))
    for x in (54, 96):
        cv.fill(
            cv.mask(ellipse(x, 76, 22, 22)),
            (246, 232, 206),
            outline=INK,
            outline_px=1.4,
        )
        cv.fill(cv.mask(ellipse(x, 78, 12, 13)), (250, 196, 60), (220, 140, 30))
        cv.fill(cv.mask(ellipse(x, 78, 6, 7)), (20, 16, 30))
        cv.fill(cv.mask(ellipse(x - 3, 74, 2.6, 2.6)), (255, 255, 255))
    cv.fill(
        cv.mask(poly([(69, 88), (81, 88), (75, 100)])),
        (240, 170, 60),
        outline=INK,
        outline_px=1,
    )
    return cv.final()


def bush(seed=3):
    rng = random.Random(seed)
    cv = Canvas(300, 210)
    cv.shadow(150, 200, 130, 12)
    base, dark = (70, 140, 96), (26, 70, 58)
    blobs = [
        (70, 140, 70, 60),
        (150, 110, 90, 80),
        (232, 140, 66, 58),
        (110, 160, 70, 46),
        (196, 162, 76, 44),
    ]
    union = None
    for cx, cy, rx, ry in blobs:
        m = cv.mask(poly(blob_points(cx, cy, rx, ry, rng, 22, 0.1)))
        cv.fill(m, shade(base, 1.18), dark, (16, 40, 40), 2)
        union = m if union is None else ImageChops.lighter(union, m)
    union = union.filter(ImageFilter.MinFilter(9))
    for _ in range(40):
        x, y = rng.uniform(40, 260), rng.uniform(50, 170)
        leaf = cv.mask(ellipse(x, y, rng.uniform(5, 10), rng.uniform(3, 6)))
        cv.fill(ImageChops.multiply(leaf, union), (150, 214, 150), alpha=0.35)
    return cv.final()


def rock(w=200, h=130, seed=5, moss=True):
    rng = random.Random(seed)
    cv = Canvas(w, h)
    cv.shadow(w / 2, h - 10, w * 0.42, 10)
    grey, dark = (150, 150, 170), (78, 78, 104)
    pts = blob_points(w / 2, h * 0.62, w * 0.42, h * 0.4, rng, 14, 0.16)
    pts = [(x, min(y, h - 10)) for x, y in pts]
    m = cv.mask(poly(pts))
    cv.fill(m, shade(grey, 1.15), dark, (36, 34, 56), 2.2)
    if moss:
        top = cv.mask(ellipse(w / 2, h * 0.36, w * 0.36, h * 0.16))
        cv.fill(ImageChops.multiply(top, m), (130, 196, 110), (80, 150, 90))
    cv.fill(
        cv.mask(ellipse(w * 0.36, h * 0.46, w * 0.1, h * 0.05)),
        (255, 255, 255),
        alpha=0.25,
    )
    return cv.final()


def campfire():
    cv = Canvas(220, 170)
    cv.shadow(110, 156, 90, 12)
    rng = random.Random(9)
    for i in range(9):  # stone ring
        a = math.pi * (0.05 + 0.9 * i / 8)
        x, y = 110 - math.cos(a) * 86, 150 - math.sin(a) * 10
        cv.fill(
            cv.mask(poly(blob_points(x, y, 16, 11, rng, 10, 0.2))),
            (140, 136, 150),
            (80, 76, 96),
            INK,
            1.6,
        )
    wood, wdark = (150, 96, 64), (86, 52, 40)
    for a, b in (
        ((40, 150), (160, 108)),
        ((180, 150), (60, 108)),
        ((70, 156), (150, 156)),
    ):
        m = cv.mask(
            lambda d, c, a=a, b=b: d.line(
                [c.s(a[0]), c.s(a[1]), c.s(b[0]), c.s(b[1])], fill=255, width=c.s(15)
            )
        )
        cv.fill(m, wood, wdark, INK, 1.8)
    return cv.final()


def _teardrop(cx, base, r, h, n=24):
    pts = [(cx, base - h)]
    for i in range(1, n):  # right flank: top point to the widest point
        t = i / n
        x = cx + r * math.sin(t * math.pi / 2) ** 0.8
        y = base - h + (h - r) * t
        pts.append((x, y))
    for i in range(n + 1):  # rounded bottom
        a = math.pi * i / n
        pts.append((cx + math.cos(a) * r, base - r + math.sin(a) * r))
    for i in range(n - 1, 0, -1):
        t = i / n
        pts.append((cx - r * math.sin(t * math.pi / 2) ** 0.8, base - h + (h - r) * t))
    return pts


def campfire_fx():
    cv = Canvas(160, 190, pad=10)
    outer = cv.mask(poly(_teardrop(80, 182, 46, 170)))
    cv.glow(outer, (255, 140, 60), 10, 0.7)
    for cx, r, h, top, bottom in (
        (80, 46, 170, (255, 150, 60), (240, 80, 40)),
        (62, 22, 90, (255, 170, 70), (250, 100, 40)),
        (100, 20, 84, (255, 170, 70), (250, 100, 40)),
        (80, 30, 118, (255, 214, 110), (255, 150, 60)),
        (80, 16, 70, (255, 250, 220), (255, 214, 130)),
    ):
        m = cv.mask(poly(_teardrop(cx, 182, r, h))).filter(
            ImageFilter.GaussianBlur(0.8 * cv.ss)
        )
        cv.fill(m, top, bottom)
    return cv.final()


def lantern():
    cv = Canvas(110, 190, pad=26)
    cv.fill(
        cv.mask(
            lambda d, c: d.line([c.s(55), 0, c.s(55), c.s(40)], fill=255, width=c.s(3))
        ),
        (60, 40, 40),
    )
    body = cv.mask(ellipse(55, 108, 42, 56))
    cv.glow(body, (255, 170, 80), 18, 0.8)
    cv.fill(body, (255, 146, 96), (214, 70, 60), (90, 30, 40), 2)
    for y in (78, 100, 122, 142):
        cv.fill(
            cv.mask(
                lambda d, c, y=y: d.arc(
                    [c.s(16), c.s(y - 20), c.s(94), c.s(y + 20)],
                    10,
                    170,
                    fill=255,
                    width=c.s(1.6),
                )
            ),
            (150, 50, 50),
            alpha=0.6,
        )
    cv.fill(
        cv.mask(poly([(30, 50), (80, 50), (74, 60), (36, 60)])),
        (60, 40, 50),
        outline=INK,
        outline_px=1.2,
    )
    cv.fill(
        cv.mask(poly([(34, 158), (76, 158), (70, 170), (40, 170)])),
        (60, 40, 50),
        outline=INK,
        outline_px=1.2,
    )
    cv.fill(cv.mask(ellipse(44, 96, 10, 22)), (255, 240, 200), alpha=0.35)
    return cv.final()


def signpost():
    cv = Canvas(200, 300)
    cv.shadow(100, 290, 50, 8)
    wood, dark = (170, 120, 80), (100, 66, 46)
    cv.fill(
        cv.mask(poly([(90, 60), (110, 60), (112, 292), (88, 292)])), wood, dark, INK, 2
    )
    for y, flip in ((80, 1), (140, -1)):
        if flip > 0:
            pts = [(30, y), (160, y), (186, y + 22), (160, y + 44), (30, y + 44)]
        else:
            pts = [(170, y), (40, y), (14, y + 22), (40, y + 44), (170, y + 44)]
        cv.fill(cv.mask(poly(pts)), shade(wood, 1.12), dark, INK, 2)
        for k in range(3):  # carved rune strokes, not words
            x0 = 58 + k * 30
            cv.fill(
                cv.mask(
                    lambda d, c, x0=x0, y=y: d.line(
                        [c.s(x0), c.s(y + 14), c.s(x0 + 14), c.s(y + 30)],
                        fill=255,
                        width=c.s(3),
                    )
                ),
                (90, 60, 44),
                alpha=0.8,
            )
    cv.fill(cv.mask(ellipse(100, 64, 26, 10)), (120, 190, 110), (80, 150, 90))
    return cv.final()


def glow_stone():
    cv = Canvas(170, 140)
    base = rock(170, 140, seed=11, moss=False)
    cv.img = base.resize(cv.img.size).convert("RGBA")
    rune = cv.mask(
        lambda d, c: [
            d.line(
                c.pts([(70, 70), (85, 55), (100, 70), (85, 95), (70, 70)]),
                fill=255,
                width=c.s(3),
            ),
            d.line(c.pts([(85, 55), (85, 100)]), fill=255, width=c.s(2.4)),
        ]
    )
    cv.glow(rune, (120, 230, 255), 8, 1.0)
    cv.fill(rune, (210, 250, 255))
    return cv.final()


def crystal():
    cv = Canvas(160, 220, pad=20)
    cv.shadow(80, 208, 60, 9)
    shards = [
        [(80, 10), (104, 80), (86, 200), (62, 200), (56, 80)],
        [(40, 70), (60, 110), (54, 200), (30, 200), (24, 110)],
        [(122, 60), (140, 110), (130, 200), (104, 200), (104, 104)],
    ]
    masks = [cv.mask(poly(p)) for p in shards]
    for m in masks:
        cv.glow(m, (170, 140, 255), 14, 0.7)
    for m, p in zip(masks, shards):
        cv.fill(m, (224, 206, 255), (120, 110, 240), (60, 40, 130), 2)
        facet = [p[0], p[1], ((p[1][0] + p[3][0]) / 2, p[3][1])]
        cv.fill(cv.mask(poly(facet)), (255, 255, 255), alpha=0.35)
    return cv.final()


def mushrooms():
    cv = Canvas(200, 140, pad=18)
    cv.shadow(100, 130, 80, 8)
    for x, h, r in ((60, 70, 30), (118, 96, 40), (158, 56, 22)):
        cv.fill(
            cv.mask(
                poly([(x - 7, 130), (x + 7, 130), (x + 5, 130 - h), (x - 5, 130 - h)])
            ),
            (236, 230, 214),
            (190, 180, 170),
            INK,
            1.4,
        )
        cap = cv.mask(
            lambda d, c, x=x, h=h, r=r: d.pieslice(
                [
                    c.s(x - r),
                    c.s(130 - h - r * 0.8),
                    c.s(x + r),
                    c.s(130 - h + r * 0.8),
                ],
                180,
                360,
                fill=255,
            )
        )
        cv.glow(cap, (110, 220, 255), 12, 0.8)
        cv.fill(cap, (150, 236, 255), (60, 140, 230), (30, 50, 110), 1.8)
        for k in range(3):
            cv.fill(
                cv.mask(
                    ellipse(
                        x - r * 0.4 + k * r * 0.4, 130 - h - r * 0.35, r * 0.1, r * 0.08
                    )
                ),
                (255, 255, 255),
                alpha=0.8,
            )
    return cv.final()


def shrine():
    cv = Canvas(170, 230, pad=12)
    cv.shadow(85, 220, 70, 10)
    stone, dark = (170, 172, 186), (96, 98, 120)
    cv.fill(
        cv.mask(poly([(40, 214), (130, 214), (122, 190), (48, 190)])),
        stone,
        dark,
        INK,
        2,
    )
    cv.fill(
        cv.mask(poly([(62, 190), (108, 190), (104, 110), (66, 110)])),
        stone,
        dark,
        INK,
        2,
    )
    light = cv.mask(poly([(72, 170), (98, 170), (96, 128), (74, 128)]))
    cv.glow(light, (255, 210, 120), 12, 0.9)
    cv.fill(light, (255, 236, 170), (255, 170, 80))
    cv.fill(
        cv.mask(poly([(20, 112), (150, 112), (118, 70), (52, 70)])), stone, dark, INK, 2
    )
    cv.fill(cv.mask(poly([(70, 70), (100, 70), (85, 40)])), stone, dark, INK, 2)
    cv.fill(cv.mask(ellipse(85, 74, 40, 8)), (130, 196, 110), (80, 150, 90))
    return cv.final()


def fallen_log():
    cv = Canvas(420, 120)
    cv.shadow(210, 110, 190, 10)
    wood, dark = (150, 104, 72), (80, 52, 40)
    cv.fill(
        cv.mask(
            lambda d, c: d.rounded_rectangle(
                [c.s(20), c.s(34), c.s(380), c.s(104)], radius=c.s(34), fill=255
            )
        ),
        shade(wood, 1.1),
        dark,
        INK,
        2.2,
    )
    cv.fill(cv.mask(ellipse(380, 69, 26, 35)), (214, 170, 120), (170, 126, 90), INK, 2)
    for r in (18, 10):
        cv.fill(
            cv.mask(
                lambda d, c, r=r: d.ellipse(
                    [c.s(380 - r * 0.7), c.s(69 - r), c.s(380 + r * 0.7), c.s(69 + r)],
                    outline=255,
                    width=c.s(1.6),
                )
            ),
            (150, 100, 70),
        )
    cv.fill(cv.mask(ellipse(160, 40, 110, 12)), (130, 196, 110), (80, 150, 90))
    return cv.final()


def rope_bridge():
    cv = Canvas(640, 150)
    rope, plank, pdark = (190, 160, 120), (170, 120, 80), (96, 66, 46)
    for i in range(22):
        x = 10 + i * 29
        sag = 16 * math.sin(math.pi * i / 21)
        cv.fill(
            cv.mask(
                poly(
                    [
                        (x, 96 + sag),
                        (x + 24, 96 + sag),
                        (x + 24, 116 + sag),
                        (x, 116 + sag),
                    ]
                )
            ),
            plank,
            pdark,
            INK,
            1.6,
        )
    for y in (40, 96):
        pts = [
            (10 + t * 620, y + 16 * math.sin(math.pi * t) + (2 if y == 96 else 0))
            for t in [k / 60 for k in range(61)]
        ]
        cv.fill(
            cv.mask(lambda d, c, pts=pts: d.line(c.pts(pts), fill=255, width=c.s(4))),
            rope,
            outline=INK,
            outline_px=0.8,
        )
    for i in range(0, 22, 3):
        x = 22 + i * 29
        sag = 16 * math.sin(math.pi * i / 21)
        cv.fill(
            cv.mask(
                lambda d, c, x=x, sag=sag: d.line(
                    [c.s(x), c.s(40 + sag), c.s(x), c.s(96 + sag)],
                    fill=255,
                    width=c.s(2.5),
                )
            ),
            rope,
        )
    for x in (4, 628):
        cv.fill(
            cv.mask(poly([(x, 20), (x + 10, 20), (x + 10, 140), (x, 140)])),
            plank,
            pdark,
            INK,
            1.6,
        )
    return cv.final()


def map_scroll():
    cv = Canvas(160, 90)
    cv.shadow(80, 80, 66, 7)
    paper, pdark = (246, 226, 180), (200, 170, 120)
    cv.fill(
        cv.mask(
            lambda d, c: d.rounded_rectangle(
                [c.s(20), c.s(26), c.s(140), c.s(70)], radius=c.s(10), fill=255
            )
        ),
        paper,
        pdark,
        INK,
        1.8,
    )
    for x in (20, 140):
        cv.fill(cv.mask(ellipse(x, 48, 12, 24)), shade(paper, 0.92), pdark, INK, 1.6)
    cv.fill(
        cv.mask(
            lambda d, c: d.line(
                [c.s(40), c.s(40), c.s(120), c.s(40)], fill=255, width=c.s(3)
            )
        ),
        (180, 60, 60),
        alpha=0.8,
    )
    return cv.final()


def glow_texture():
    size = 256
    img = radial_layer(
        (size, size), (size / 2, size / 2), size / 2, (255, 255, 255, 255), power=2.2
    )
    return img


def spark_texture():
    cv = Canvas(64, 64)
    m = cv.mask(
        lambda d, c: d.polygon(
            c.pts(
                [
                    (32, 2),
                    (38, 26),
                    (62, 32),
                    (38, 38),
                    (32, 62),
                    (26, 38),
                    (2, 32),
                    (26, 26),
                ]
            ),
            fill=255,
        )
    )
    cv.glow(m, (255, 255, 255), 4, 0.9)
    cv.fill(m, (255, 255, 255))
    return cv.final()


def smoke_texture():
    rng = random.Random(4)
    cv = Canvas(128, 128)
    m = cv.mask(
        lambda d, c: [
            d.ellipse([c.s(x - r), c.s(y - r), c.s(x + r), c.s(y + r)], fill=200)
            for x, y, r in ((64, 64, 40), (44, 70, 26), (86, 58, 28), (60, 44, 24))
        ]
    )
    m = m.filter(ImageFilter.GaussianBlur(8 * cv.ss))
    cv.paint_solid(m, (235, 232, 245))
    del rng
    return cv.final()


BUILDERS = {
    "frog": frog,
    "rabbit": rabbit,
    "butterfly": butterfly,
    "owl": owl,
    "bush": bush,
    "rock": lambda: rock(),
    "boulder": lambda: rock(340, 240, seed=21),
    "campfire": campfire,
    "campfire_fx": campfire_fx,
    "lantern": lantern,
    "signpost": signpost,
    "glow_stone": glow_stone,
    "crystal": crystal,
    "mushrooms": mushrooms,
    "shrine": shrine,
    "fallen_log": fallen_log,
    "rope_bridge": rope_bridge,
    "map_scroll": map_scroll,
    "_glow": glow_texture,
    "_spark": spark_texture,
    "_smoke": smoke_texture,
}


def main(only: list[str] | None = None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, build in BUILDERS.items():
        if only and name not in only:
            continue
        img = build()
        path = OUT / f"{name.lstrip('_')}.png"
        img.save(path, optimize=True)
        print(f"{path.relative_to(ROOT)} {img.size} {path.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main(sys.argv[1:] or None)
