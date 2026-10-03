"""The Kingdom: the build that fills a whole 11 hour stream (and the next).

256 lots on a 16 x 16 grid north of the base, built from the middle outward
so the kingdom visibly grows: houses, gardens, towers, castles and dragon
statues in many colours, each lot with a path around it. About 160,000
blocks, every one laid by hand, about 10 hours. Progress is kept in the
project state, so the next stream goes on where this one stopped. Anything
chat asks for comes first; then they go back to the kingdom.
"""

from __future__ import annotations

import random
from typing import Any

from .minecraft_templates import build as make_template

GRID = 16  # 16 x 16 lots
LOT = 45  # blocks between lot centres
CENTER = (0, -160)  # x, z from the base: north of the big projects
SEED = 20261004

KINDS = (  # template, sizes, weight
    ("house", ("medium", "large"), 40),
    ("garden", ("medium", "large"), 16),
    ("tower", ("medium", "large"), 18),
    ("castle", ("medium", "large"), 8),
    ("dragon", ("medium",), 5),
)
PALETTES = {
    "house": [("spruce_planks", "dark_oak_planks"), ("birch_planks", "spruce_planks"), ("oak_planks", "bricks"),
              ("cherry_planks", "dark_oak_planks"), ("white_concrete", "blue_terracotta"),
              ("sandstone", "red_sandstone"), ("mud_bricks", "spruce_planks"), ("stone_bricks", "deepslate_tiles")],
    "garden": [("stone_bricks", "dirt_path"), ("quartz_block", "gravel"), ("mossy_stone_bricks", "dirt_path"),
               ("polished_blackstone_bricks", "gravel")],
    "tower": [("stone_bricks", "polished_andesite"), ("deepslate_bricks", "polished_deepslate"),
              ("sandstone", "smooth_sandstone"), ("quartz_bricks", "smooth_quartz"), ("bricks", "spruce_planks")],
    "castle": [("stone_bricks", "polished_andesite"), ("quartz_block", "smooth_quartz"),
               ("deepslate_bricks", "polished_blackstone"), ("prismarine_bricks", "dark_prismarine")],
    "dragon": [("green_concrete", "lime_terracotta"), ("red_concrete", "orange_terracotta"),
               ("black_concrete", "purple_concrete"), ("light_blue_concrete", "white_concrete"),
               ("gold_block", "yellow_terracotta")],
}
TITLES = {"house": "a house", "garden": "a garden", "tower": "a tower", "castle": "a castle", "dragon": "a dragon statue"}


def lots() -> list[dict[str, Any]]:
    """Every lot of the kingdom, in building order (the middle first)."""
    rng = random.Random(SEED)
    half = GRID // 2
    cells = [(i, j) for i in range(-half, half) for j in range(-half, half)]
    cells.sort(key=lambda c: ((c[0] + 0.5) ** 2 + (c[1] + 0.5) ** 2, c))
    names, weights = zip(*((k[0], k[2]) for k in KINDS))
    sizes = {k[0]: k[1] for k in KINDS}
    out = []
    for n, (i, j) in enumerate(cells):
        kind = "castle" if n == 0 else rng.choices(names, weights)[0]  # the king's castle in the middle
        size = "large" if n == 0 else rng.choice(sizes[kind])
        main, accent = rng.choice(PALETTES[kind])
        out.append({
            "n": n, "template": kind, "size": size, "main": main, "accent": accent,
            "x": CENTER[0] + i * LOT, "z": CENTER[1] + j * LOT,
            "title": "the king's castle" if n == 0 else TITLES[kind],
            "seed": SEED + n,
        })
    return out


def lot_blocks(lot: dict[str, Any]) -> dict[tuple[int, int, int], str]:
    """The lot's build (relative, as in a free build) and a path around it."""
    spots = make_template(lot["template"], lot["size"], lot["main"], lot["accent"], seed=lot["seed"])
    if not spots:
        return spots
    xs = [x for x, _y, _z in spots]
    zs = [z for _x, _y, z in spots]
    x1, x2, z1, z2 = min(xs) - 2, max(xs) + 2, min(zs) - 2, max(zs) + 2
    for x in range(x1, x2 + 1):  # a path all round, in the ground
        for z in (z1, z2):
            spots.setdefault((x, -1, z), "dirt_path")
    for z in range(z1, z2 + 1):
        for x in (x1, x2):
            spots.setdefault((x, -1, z), "dirt_path")
    return spots


def total_blocks() -> int:
    return sum(len(lot_blocks(lot)) for lot in lots())
