"""Detailed builds for what chat asks most: a garden, a house, a tower, a
castle (on the ground or floating in the sky).

The AI only picks the template, the size and the colours; the details come
from here, so a "flower garden" really has a lawn, paths, flower beds in
colour groups, hedges, a fountain and lanterns, not three blocks.

Coordinates: x left (-) / right (+), y up (0 = the first layer above the
ground), z forward (away from where the girls start). No block has a facing
state, so a build can be turned to any direction.
"""

from __future__ import annotations

import random
from typing import Callable

Spots = dict[tuple[int, int, int], str]
SIZES = {"small": 0, "medium": 1, "large": 2}
FLOWERS = {
    "warm": ["poppy", "red_tulip", "orange_tulip", "dandelion", "rose_bush"],
    "cool": ["cornflower", "blue_orchid", "allium", "lilac", "azure_bluet"],
    "soft": ["pink_tulip", "white_tulip", "oxeye_daisy", "lily_of_the_valley", "peony"],
}
LEAVES = "oak_leaves[persistent=true]"


def _box(out: Spots, a: tuple[int, int, int], b: tuple[int, int, int], block: str, walls: bool = False) -> None:
    (x1, y1, z1), (x2, y2, z2) = a, b
    for y in range(min(y1, y2), max(y1, y2) + 1):
        for x in range(min(x1, x2), max(x1, x2) + 1):
            for z in range(min(z1, z2), max(z1, z2) + 1):
                if walls and min(x1, x2) < x < max(x1, x2) and min(z1, z2) < z < max(z1, z2):
                    continue
                out[(x, y, z)] = block


def garden(size: int, main: str, accent: str, rng: random.Random) -> Spots:
    w = 5 + 2 * size  # half width: 11, 15 or 19 across
    d = 2 * w
    cz = w
    out: Spots = {}
    _box(out, (-w, 0, 0), (w, 0, d), "grass_block")
    for x in range(-w, w + 1):  # paths: a cross through the middle
        out[(x, 0, cz)] = "gravel" if accent == "gravel" else "dirt_path"
    for z in range(0, d + 1):
        out[(0, 0, z)] = "gravel" if accent == "gravel" else "dirt_path"
    themes = list(FLOWERS)
    rng.shuffle(themes)
    for x in range(-w + 1, w):
        for z in range(1, d):
            if x == 0 or z == cz or abs(x) <= 2 and abs(z - cz) <= 2:
                continue
            quarter = (x > 0) * 2 + (z > cz)
            flowers = FLOWERS[themes[quarter % len(themes)]]
            if rng.random() < 0.55:
                flower = rng.choice(flowers)
                if flower in ("rose_bush", "lilac", "peony"):
                    flower = flowers[0]  # tall flowers need two blocks: keep it simple
                out[(x, 1, z)] = flower
    for x in range(-w, w + 1):  # a hedge all round, open where the paths go out
        for z in (0, d):
            if x != 0:
                out[(x, 1, z)] = LEAVES
    for z in range(0, d + 1):
        for x in (-w, w):
            if z != cz:
                out[(x, 1, z)] = LEAVES
    for x in range(-2, 3):  # the fountain in the middle
        for z in range(cz - 2, cz + 3):
            out[(x, 0, z)] = main
            out[(x, 1, z)] = main if max(abs(x), abs(z - cz)) == 2 else "water"
    out[(0, 1, cz)] = main
    out[(0, 2, cz)] = main
    out[(0, 3, cz)] = "sea_lantern"
    for x, z in ((-w, 0), (w, 0), (-w, d), (w, d), (-w, cz), (w, cz)):  # lantern posts
        out[(x, 1, z)] = "oak_fence"
        out[(x, 2, z)] = "oak_fence"
        out[(x, 3, z)] = "lantern"
    return out


def house(size: int, main: str, accent: str, rng: random.Random) -> Spots:
    w = 3 + size  # 7, 9 or 11 wide
    d = 6 + 2 * size
    h = 4 + size
    out: Spots = {}
    _box(out, (-w, 0, 0), (w, 0, d), "cobblestone")
    _box(out, (-w + 1, 0, 1), (w - 1, 0, d - 1), "oak_planks")
    for y in range(1, h + 1):
        _box(out, (-w, y, 0), (w, y, d), main, walls=True)
        for x, z in ((-w, 0), (w, 0), (-w, d), (w, d)):
            out[(x, y, z)] = "oak_log"
    for y in (1, 2):  # the door: an opening in the front wall
        out.pop((0, y, 0), None)
    for y in (2, 3):  # windows on every side
        for x in range(-w + 2, w - 1, 3):
            if x != 0:
                out[(x, y, 0)] = "glass_pane"
            out[(x, y, d)] = "glass_pane"
        for z in range(2, d - 1, 3):
            out[(-w, y, z)] = "glass_pane"
            out[(w, y, z)] = "glass_pane"
    roof = accent if accent.endswith(("planks", "bricks", "tiles")) else "dark_oak_planks"
    layer = 0
    while w + 1 - layer >= 0 and d + 2 - 2 * layer >= 0:  # a stepped roof with an overhang
        x1, x2 = -w - 1 + layer, w + 1 - layer
        z1, z2 = -1 + layer, d + 1 - layer
        if x1 > x2 or z1 > z2:
            break
        _box(out, (x1, h + 1 + layer, z1), (x2, h + 1 + layer, z2), roof, walls=x2 - x1 > 2 and z2 - z1 > 2)
        layer += 1
    for y in range(h + 1, h + 4):  # a chimney
        out[(w - 1, y, d - 1)] = "bricks"
    for x in (-2, 2):  # flower boxes by the door
        out[(x, 0, -1)] = "grass_block"
        out[(x, 1, -1)] = rng.choice(FLOWERS["warm"][:3])
    return out


def tower(size: int, main: str, accent: str, rng: random.Random) -> Spots:
    w = 2 + size  # 5, 7 or 9 across
    h = 12 + 4 * size
    out: Spots = {}
    _box(out, (-w, 0, 0), (w, 0, 2 * w), main)
    for y in range(1, h + 1):
        _box(out, (-w, y, 0), (w, y, 2 * w), main, walls=True)
    for y in range(3, h, 4):  # windows up the tower
        out[(0, y, 0)] = "glass_pane"
        out[(0, y, 2 * w)] = "glass_pane"
        out[(-w, y, w)] = "glass_pane"
        out[(w, y, w)] = "glass_pane"
    out.pop((0, 1, 0), None)
    out.pop((0, 2, 0), None)
    _box(out, (-w - 1, h + 1, -1), (w + 1, h + 1, 2 * w + 1), accent)
    for x in range(-w - 1, w + 2):  # battlements
        for z in (-1, 2 * w + 1):
            if (x + w) % 2 == 0:
                out[(x, h + 2, z)] = main
    for z in range(-1, 2 * w + 2):
        for x in (-w - 1, w + 1):
            if (z + 1) % 2 == 0:
                out[(x, h + 2, z)] = main
    for x, z in ((-w, 0), (w, 0), (-w, 2 * w), (w, 2 * w)):
        out[(x, h + 2, z)] = "lantern"
    out[(0, h + 2, w)] = accent
    out[(0, h + 3, w)] = "red_wool"  # a flag
    out[(1, h + 3, w)] = "red_wool"
    out[(0, h + 4, w)] = "red_wool"
    return out


def castle(size: int, main: str, accent: str, rng: random.Random, sky: bool = False) -> Spots:
    w = 6 + size  # 13, 15 or 17 across
    d = 2 * w
    base = 12 if sky else 0
    out: Spots = {}
    _box(out, (-w, base, 0), (w, base, d), accent)  # the courtyard floor
    if sky:  # a floating island under it
        _box(out, (-w + 1, base - 1, 1), (w - 1, base - 1, d - 1), "grass_block")
        _box(out, (-w + 3, base - 2, 3), (w - 3, base - 2, d - 3), "dirt")
        _box(out, (-w + 5, base - 3, 5), (w - 5, base - 3, d - 5), "stone")
        _box(out, (-2, base - 4, w - 2), (2, base - 4, w + 2), "stone")
    for y in range(base + 1, base + 4):  # the outer wall
        _box(out, (-w, y, 0), (w, y, d), main, walls=True)
    for x in range(-w, w + 1, 2):
        out[(x, base + 4, 0)] = main
        out[(x, base + 4, d)] = main
    for z in range(0, d + 1, 2):
        out[(-w, base + 4, z)] = main
        out[(w, base + 4, z)] = main
    for y in range(base + 1, base + 4):  # the gate
        for x in (-1, 0, 1):
            out.pop((x, y, 0), None)
    for cx, cz in ((-w, 0), (w, 0), (-w, d), (w, d)):  # four corner towers
        for y in range(base + 1, base + 7):
            _box(out, (cx - 1, y, cz - 1), (cx + 1, y, cz + 1), main, walls=True)
        for x, z in ((cx - 1, cz - 1), (cx + 1, cz - 1), (cx - 1, cz + 1), (cx + 1, cz + 1)):
            out[(x, base + 7, z)] = main
        out[(cx, base + 7, cz)] = "lantern"
    k = 2 + size // 2  # the keep in the middle
    for y in range(base + 1, base + 9):
        _box(out, (-k, y, w - k), (k, y, w + k), main, walls=True)
    for y in (base + 3, base + 6):
        out[(0, y, w - k)] = "glass_pane"
    out.pop((0, base + 1, w - k), None)
    out.pop((0, base + 2, w - k), None)
    _box(out, (-k - 1, base + 9, w - k - 1), (k + 1, base + 9, w + k + 1), accent)
    for y in range(base + 10, base + 13):
        out[(0, y, w)] = "oak_fence"
    out[(0, base + 13, w)] = "red_wool"
    out[(1, base + 13, w)] = "red_wool"
    out[(1, base + 12, w)] = "red_wool"
    return out


def _ellipsoid(out: Spots, c: tuple[float, float, float], r: tuple[float, float, float], block: str,
               belly: str = "", shell: bool = True) -> None:
    """A rounded shape (only its skin when shell: it looks solid and costs far fewer blocks)."""
    cx, cy, cz = c
    rx, ry, rz = (max(0.6, v) for v in r)
    for x in range(int(cx - rx) - 1, int(cx + rx) + 2):
        for y in range(int(cy - ry) - 1, int(cy + ry) + 2):
            for z in range(int(cz - rz) - 1, int(cz + rz) + 2):
                d = ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 + ((z - cz) / rz) ** 2
                if d > 1.0:
                    continue
                if shell:  # inside, not touching the outside: skip
                    inner = ((x - cx) / max(rx - 1, 0.5)) ** 2 + ((y - cy) / max(ry - 1, 0.5)) ** 2 + \
                        ((z - cz) / max(rz - 1, 0.5)) ** 2
                    if inner < 1.0 and rx > 1.5 and ry > 1.5 and rz > 1.5:
                        continue
                if y < 0:
                    continue
                out[(x, y, z)] = belly if belly and y < cy - ry * 0.35 else block


def _line(out: Spots, a: tuple[float, float, float], b: tuple[float, float, float], block: str) -> None:
    steps = int(max(abs(b[0] - a[0]), abs(b[1] - a[1]), abs(b[2] - a[2]))) + 1
    for i in range(steps + 1):
        t = i / steps
        p = (round(a[0] + (b[0] - a[0]) * t), round(a[1] + (b[1] - a[1]) * t), round(a[2] + (b[2] - a[2]) * t))
        if p[1] >= 0:
            out[p] = block


def dragon(size: int, main: str, accent: str, rng: random.Random) -> Spots:
    """A dragon statue facing the girls: head and neck in front, body,
    spread wings, four legs with claws, a long curling tail, back spikes."""
    k = (0.75, 1.0, 1.4)[size]

    def p(x: float, y: float, z: float) -> tuple[float, float, float]:
        return x * k, y * k, z * k

    out: Spots = {}
    wing = "red_wool" if main not in ("red_wool",) else "black_wool"
    if accent in (main,):
        accent = "yellow_terracotta"
    # body, with a lighter belly
    _ellipsoid(out, p(0, 6, 14), (4 * k, 3.8 * k, 7.5 * k), main, belly=accent)
    # legs and claws
    for sx in (-1, 1):
        for lz in (9, 18):
            x, z = round(3 * sx * k), round(lz * k)
            for y in range(0, round(4 * k) + 1):
                for dx in (0, sx):
                    for dz in (0, 1):
                        out[(x + dx, y, z + dz)] = main
            for dx in (-1, 0, 1):
                out[(x + dx, 0, z - 1)] = "quartz_block"  # claws
    # neck: a curve of rounded pieces up and forward
    for i in range(6):
        t = i / 5
        _ellipsoid(out, p(0, 8 + 6 * t, 8 - 5 * t), (2.2 * k, 2.2 * k, 2.2 * k), main, belly=accent)
    # head, snout, eyes, horns, teeth
    _ellipsoid(out, p(0, 15, 1.5), (2.6 * k, 2.2 * k, 3 * k), main)
    _ellipsoid(out, p(0, 14, -2), (1.7 * k, 1.3 * k, 2.2 * k), main)
    for sx in (-1, 1):
        ex, ey, ez = p(2.4 * sx, 16, 0.5)
        out[(round(ex), round(ey), round(ez))] = "gold_block"  # glowing eyes
        out[(round(ex), round(ey) - 1, round(ez))] = "black_concrete"
        _line(out, p(1.4 * sx, 17, 2), p(2.4 * sx, 20, 5), "quartz_block")  # horns
        _line(out, p(1.4 * sx, 16.5, 3), p(2 * sx, 18.5, 6), "quartz_block")
        tx, ty, tz = p(1 * sx, 12.8, -3)
        out[(round(tx), round(ty), round(tz))] = "quartz_block"  # teeth
        out[(round(tx), round(ty), round(tz) + 1)] = "quartz_block"
    nx, ny, nz = p(0, 14.6, -4)
    out[(round(nx) - 1, round(ny), round(nz))] = "black_concrete"  # nostrils
    out[(round(nx) + 1, round(ny), round(nz))] = "black_concrete"
    # wings: a bone from the shoulder to the tip, the skin hanging behind it
    for sx in (-1, 1):
        shoulder = p(3.5 * sx, 9, 11)
        tip = p(16 * sx, 18, 14)
        _line(out, shoulder, tip, main)
        for i in range(0, 13):
            t = i / 12
            bone = (shoulder[0] + (tip[0] - shoulder[0]) * t, shoulder[1] + (tip[1] - shoulder[1]) * t,
                    shoulder[2] + (tip[2] - shoulder[2]) * t)
            edge = (bone[0], 8 * k + (bone[1] - 8 * k) * (0.35 + 0.5 * t), 20 * k - 4 * k * t)
            _line(out, bone, edge, wing)
        for f in (0.35, 0.65, 1.0):  # finger bones through the skin
            bone = (shoulder[0] + (tip[0] - shoulder[0]) * f, shoulder[1] + (tip[1] - shoulder[1]) * f,
                    shoulder[2] + (tip[2] - shoulder[2]) * f)
            _line(out, bone, (bone[0], 8 * k + (bone[1] - 8 * k) * (0.35 + 0.5 * f), 20 * k - 4 * k * f), main)
    # tail: thinner and thinner, curling to the side at the end
    for i in range(10):
        t = i / 9
        x = 5 * t * t * (1 if rng.random() < 2 else -1)
        _ellipsoid(out, p(x, 5 - 3 * t, 21 + 11 * t), ((2.4 - 1.6 * t) * k,) * 3, main, belly=accent if t < 0.5 else "")
    # spikes along the back and the tail
    for i in range(12):
        t = i / 11
        z = (5 + 25 * t)
        y = 11 - 3 * t if z < 21 else 7.5 - 3 * (z - 21) / 11
        x = 5 * ((z - 21) / 11) ** 2 if z > 21 else 0
        sx, sy, sz = p(x, y, z)
        _line(out, (sx, sy, sz), (sx, sy + 1.5 * k, sz + 0.5), "quartz_block" if accent == "quartz_block" else accent)
    return out


TEMPLATES: dict[str, Callable[..., Spots]] = {
    "garden": garden, "house": house, "tower": tower, "castle": castle, "dragon": dragon,
}
DEFAULT_COLOURS = {
    "garden": ("stone_bricks", "dirt_path"), "house": ("spruce_planks", "dark_oak_planks"),
    "tower": ("stone_bricks", "polished_andesite"), "castle": ("stone_bricks", "polished_andesite"),
    "dragon": ("green_concrete", "lime_terracotta"),
}


def build(template: str, size: str, main: str, accent: str, sky: bool = False, seed: int = 0) -> Spots:
    """The blocks of a template (relative), or {} for an unknown template."""
    make = TEMPLATES.get(template)
    if make is None:
        return {}
    rng = random.Random(seed)
    level = SIZES.get(size, 1)
    if template == "castle":
        return make(level, main, accent, rng, sky=sky)
    out = make(level, main, accent, rng)
    if sky:  # anything can float: lift it and put an island under it
        lifted = {(x, y + 12, z): b for (x, y, z), b in out.items()}
        xs = [x for x, _y, _z in out] or [0]
        zs = [z for _x, _y, z in out] or [0]
        _box(lifted, (min(xs), 11, min(zs)), (max(xs), 11, max(zs)), "grass_block")
        _box(lifted, (min(xs) + 2, 10, min(zs) + 2), (max(xs) - 2, 10, max(zs) - 2), "dirt")
        _box(lifted, (min(xs) + 4, 9, min(zs) + 4), (max(xs) - 4, 9, max(zs) - 4), "stone")
        return lifted
    return out


def footprint(spots: Spots) -> tuple[int, int, int, int, int]:
    """x1, x2, z1, z2, top of a build (for clearing its plot)."""
    if not spots:
        return 0, 0, 0, 0, 0
    xs = [x for x, _y, _z in spots]
    ys = [y for _x, y, _z in spots]
    zs = [z for _x, _y, z in spots]
    return min(xs), max(xs), min(zs), max(zs), max(ys)
