"""The long goals of the Minecraft show: a castle that grows, a giant neural
network built from scratch, then a green farm, a flower garden and pets.

The cheap AI brains cannot build a castle block by block, so the work is
split: Mika and Luna gather (their inventory and time count as progress) and
every finished milestone adds that part to their world through the server
console. Viewers watch a progress bar, the girls celebrate each step, and the
next step becomes their new goal.

    VR_MINECRAFT_MILESTONE_MINUTES=12   about how long one milestone takes

Progress is saved in data/minecraft_project.json, so a restart continues the
same castle in the same place.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

STATE_FILE = Path("data/minecraft_project.json")
VIEW_SPOT = (24, -4)  # x, z from the base: outside every build, with a view of the castle
LOADED = (-24, -36, 52, 32)  # x1, z1, x2, z2 from the base: every build is inside this box
REFUSED = ("Unknown", "Incorrect", "Invalid", "Expected", "error", "Could not", "not loaded")

# Each milestone: name, what the girls should gather meanwhile, and the build
# (commands with x, y, z relative to the base; "{X}" style placeholders are
# filled with absolute numbers). Fill boxes stay below 32768 blocks.
Build = list[str]


def _castle_walls() -> Build:
    return [
        # a flat green site first, so the castle stands on clean ground
        "fill {x-15} {y-1} {z-15} {x15} {y-1} {z15} minecraft:grass_block",
        "fill {x-15} {y} {z-15} {x15} {y16} {z15} minecraft:air",
        "fill {x-9} {y} {z-9} {x9} {y4} {z9} minecraft:stone_bricks hollow",
        "fill {x-8} {y4} {z-8} {x8} {y4} {z8} minecraft:air",
        "fill {x-8} {y} {z-8} {x8} {y} {z8} minecraft:polished_andesite",
        "fill {x-1} {y1} {z-9} {x1} {y3} {z-9} minecraft:air",
    ]


def _castle_towers() -> Build:
    out = []
    for cx, cz in ((-9, -9), (9, -9), (-9, 9), (9, 9)):
        out += [
            f"fill {{x{cx - 2}}} {{y}} {{z{cz - 2}}} {{x{cx + 2}}} {{y8}} {{z{cz + 2}}} minecraft:stone_bricks hollow",
            f"fill {{x{cx - 1}}} {{y8}} {{z{cz - 1}}} {{x{cx + 1}}} {{y8}} {{z{cz + 1}}} minecraft:air",
            f"setblock {{x{cx}}} {{y5}} {{z{cz - 2}}} minecraft:glass_pane",
            f"setblock {{x{cx}}} {{y5}} {{z{cz + 2}}} minecraft:glass_pane",
        ]
    return out


def _castle_battlements() -> Build:
    out = []
    for i in range(-8, 9, 2):
        for x, z in ((i, -9), (i, 9), (-9, i), (9, i)):
            out.append(f"setblock {{x{x}}} {{y5}} {{z{z}}} minecraft:stone_brick_wall")
    for cx, cz in ((-9, -9), (9, -9), (-9, 9), (9, 9)):
        for dx, dz in ((-2, -2), (2, -2), (-2, 2), (2, 2), (0, -2), (0, 2), (-2, 0), (2, 0)):
            out.append(f"setblock {{x{cx + dx}}} {{y9}} {{z{cz + dz}}} minecraft:stone_brick_wall")
    return out


def _castle_keep() -> Build:
    return [
        "fill {x-3} {y1} {z-3} {x3} {y11} {z3} minecraft:stone_bricks hollow",
        "fill {x-2} {y1} {z-2} {x2} {y10} {z2} minecraft:air",
        "fill {x-3} {y12} {z-3} {x3} {y12} {z3} minecraft:dark_oak_planks",
        "fill {x-2} {y13} {z-2} {x2} {y13} {z2} minecraft:dark_oak_planks",
        "fill {x-1} {y14} {z-1} {x1} {y14} {z1} minecraft:dark_oak_planks",
        "setblock {x0} {y15} {z0} minecraft:dark_oak_planks",
        "fill {x-1} {y5} {z-3} {x1} {y6} {z-3} minecraft:glass_pane",
        "fill {x-1} {y5} {z3} {x1} {y6} {z3} minecraft:glass_pane",
        "fill {x-3} {y5} {z-1} {x-3} {y6} {z1} minecraft:glass_pane",
        "fill {x3} {y5} {z-1} {x3} {y6} {z1} minecraft:glass_pane",
        "fill {x0} {y1} {z-3} {x0} {y2} {z-3} minecraft:air",
    ]


def _castle_flags() -> Build:
    out = [
        "setblock {x0} {y1} {z-9} minecraft:oak_fence_gate[facing=south]",
        "fill {x-1} {y1} {z-11} {x1} {y1} {z-11} minecraft:lantern",
        "setblock {x0} {y16} {z0} minecraft:red_banner",
    ]
    for cx, cz in ((-9, -9), (9, -9), (-9, 9), (9, 9)):
        out.append(f"setblock {{x{cx}}} {{y9}} {{z{cz}}} minecraft:red_banner")
        out.append(f"setblock {{x{cx}}} {{y1}} {{z{cz}}} minecraft:lantern")
    return out


def _farm_fields() -> Build:
    # south of the castle, outside the gate
    return [
        "fill {x-12} {y-1} {z-26} {x12} {y-1} {z-14} minecraft:grass_block",
        "fill {x-12} {y} {z-26} {x12} {y6} {z-14} minecraft:air",
        "fill {x-10} {y-1} {z-24} {x10} {y-1} {z-16} minecraft:farmland[moisture=7]",
        "fill {x-10} {y-1} {z-20} {x10} {y-1} {z-20} minecraft:water",
        "fill {x-10} {y} {z-24} {x-1} {y} {z-21} minecraft:wheat[age=7]",
        "fill {x1} {y} {z-24} {x10} {y} {z-21} minecraft:carrots[age=7]",
        "fill {x-10} {y} {z-19} {x-1} {y} {z-16} minecraft:potatoes[age=7]",
        "fill {x1} {y} {z-19} {x10} {y} {z-16} minecraft:beetroots[age=3]",
    ]


def _farm_fence_barn() -> Build:
    return [
        "fill {x-12} {y} {z-26} {x12} {y} {z-26} minecraft:oak_fence",
        "fill {x-12} {y} {z-26} {x-12} {y} {z-14} minecraft:oak_fence",
        "fill {x12} {y} {z-26} {x12} {y} {z-14} minecraft:oak_fence",
        "fill {x13} {y} {z-26} {x19} {y5} {z-20} minecraft:spruce_planks hollow",
        "fill {x14} {y1} {z-25} {x18} {y4} {z-21} minecraft:air",
        "fill {x16} {y1} {z-20} {x16} {y2} {z-20} minecraft:air",
        "fill {x13} {y6} {z-26} {x19} {y6} {z-20} minecraft:red_terracotta",
        "setblock {x16} {y3} {z-23} minecraft:lantern",
    ]


def _farm_animals() -> Build:
    out = []
    for i, (mob, n) in enumerate((("cow", 3), ("sheep", 3), ("chicken", 4), ("pig", 2))):
        for k in range(n):
            out.append(f"summon minecraft:{mob} {{x{-9 + i * 5 + k}}} {{y1}} {{z-15}} {{PersistenceRequired:1b}}")
    return out


def _garden_flowers() -> Build:
    # north of the castle
    out = [
        "fill {x-14} {y-1} {z14} {x14} {y-1} {z28} minecraft:grass_block",
        "fill {x-14} {y} {z14} {x14} {y8} {z28} minecraft:air",
        "fill {x-1} {y-1} {z10} {x1} {y-1} {z28} minecraft:dirt_path",
    ]
    flowers = ["poppy", "dandelion", "cornflower", "allium", "oxeye_daisy", "pink_tulip", "azure_bluet", "lily_of_the_valley"]
    for i, flower in enumerate(flowers):
        z = 15 + i * 1.6
        out.append(f"fill {{x-12}} {{y}} {{z{int(z)}}} {{x-3}} {{y}} {{z{int(z)}}} minecraft:{flower}")
        out.append(f"fill {{x3}} {{y}} {{z{int(z)}}} {{x12}} {{y}} {{z{int(z)}}} minecraft:{flowers[-1 - i]}")
    return out


def _garden_pond_trees() -> Build:
    return [
        "fill {x5} {y-2} {z22} {x11} {y-1} {z27} minecraft:water",
        "setblock {x7} {y} {z24} minecraft:lily_pad",
        "setblock {x9} {y} {z25} minecraft:lily_pad",
        "place feature minecraft:azalea_tree {x-9} {y} {z25}",
        "place feature minecraft:birch {x-5} {y} {z27}",
        "place feature minecraft:fancy_oak {x12} {y} {z16}",
        "fill {x-2} {y} {z20} {x-2} {y} {z20} minecraft:lantern",
        "fill {x2} {y} {z20} {x2} {y} {z20} minecraft:lantern",
    ]


# ---------------------------------------------------------------- neural network
# A real digit reading network (digit_net.py), drawn by minecraft_net_show.py.
def _net_input() -> Build:
    from .minecraft_net_show import build_input

    return build_input()


def _net_hidden() -> Build:
    from .minecraft_net_show import build_hidden

    return build_hidden()


def _net_output() -> Build:
    from .minecraft_net_show import build_output

    return build_output()


def _net_weights() -> Build:
    from .minecraft_net_show import build_weights

    return build_weights()


def _net_training() -> Build:
    from .minecraft_net_show import build_trained

    return build_trained()


_net_training.timed = True  # type: ignore[attr-defined]  # creative: no pieces, the network trains for the whole part


def _pets() -> Build:
    # pets for both girls: tamed by their (offline) player id
    return [
        'summon minecraft:wolf {x-2} {y1} {z0} {Owner:%MIKA%,CustomName:"Biscuit",PersistenceRequired:1b}',
        'summon minecraft:cat {x2} {y1} {z0} {Owner:%LUNA%,CustomName:"Mochi",PersistenceRequired:1b,variant:"minecraft:calico"}',
        'summon minecraft:fox {x0} {y1} {z18} {CustomName:"Pumpkin",PersistenceRequired:1b}',
        'summon minecraft:rabbit {x3} {y1} {z18} {CustomName:"Bun Bun",PersistenceRequired:1b}',
        'summon minecraft:parrot {x-3} {y2} {z18} {CustomName:"Kiwi",PersistenceRequired:1b}',
    ]


PROJECTS: list[dict[str, Any]] = [
    {
        "id": "castle",
        "title": "🏰 The Castle",
        "milestones": [
            ("Castle walls", "gather lots of stone and cobblestone for the castle walls", _castle_walls),
            ("Four towers", "gather more stone and wood for the four towers", _castle_towers),
            ("Battlements", "gather stone for the battlements on the walls", _castle_battlements),
            ("The keep", "gather wood and glass materials (sand) for the big keep in the middle", _castle_keep),
            ("Gate and flags", "gather wool or anything colorful for the flags and the gate", _castle_flags),
        ],
    },
    {
        "id": "neural_net",
        "title": "🧠 Neural Network From Scratch",
        "milestones": [
            ("Input layer", "gather stone and sand for the lab floor and the 35 pixel input board", _net_input),
            ("Hidden layers", "gather more stone and wood for the sixteen hidden neurons", _net_hidden),
            ("Output layer", "gather wood and stone for the ten output neurons, one for each digit", _net_output),
            ("Weights", "gather lots of sand for the glass weights that connect every neuron", _net_weights),
            ("Training", "gather anything useful while the network really trains with backpropagation, and argue about whether it is learning", _net_training),
        ],
    },
    {
        "id": "farm",
        "title": "🌾 The Green Farm",
        "milestones": [
            ("Crop fields", "gather seeds and food for the farm fields outside the castle gate", _farm_fields),
            ("Fence and barn", "gather wood for the fence and the barn", _farm_fence_barn),
            ("Farm animals", "find animals and food for the farm", _farm_animals),
        ],
    },
    {
        "id": "garden",
        "title": "🌸 The Flower Garden",
        "milestones": [
            ("Flower beds", "collect flowers for the garden behind the castle", _garden_flowers),
            ("Pond and blossom trees", "collect saplings and anything pretty for the garden pond", _garden_pond_trees),
        ],
    },
    {
        "id": "pets",
        "title": "🐶 Pets",
        "milestones": [
            ("Adopt pets", "get bones and fish so you can take care of your new pets", _pets),
        ],
    },
]


def offline_uuid_ints(name: str) -> str:
    """The NBT int array of an offline mode player's id (how pets know their owner)."""
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode()).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30
    digest[8] = (digest[8] & 0x3F) | 0x80
    value = uuid.UUID(bytes=bytes(digest)).int
    parts = [(value >> shift) & 0xFFFFFFFF for shift in (96, 64, 32, 0)]
    signed = [p - (1 << 32) if p >= 1 << 31 else p for p in parts]
    return "[I;" + ",".join(str(p) for p in signed) + "]"


def place(command: str, base: tuple[int, int, int], owners: dict[str, str]) -> str:
    """'{x-9} {y4}' style offsets around the base -> absolute numbers."""
    import re

    x0, y0, z0 = base

    def repl(match: "re.Match[str]") -> str:
        axis, offset = match.group(1), match.group(2)
        number = int(offset) if offset else 0
        return str({"x": x0, "y": y0, "z": z0}[axis] + number)

    out = re.sub(r"\{([xyz])(-?\d*)\}", repl, command)
    for key, value in owners.items():
        out = out.replace(f"%{key.upper()}%", value)
    return out


# ---------------------------------------------------------------- creative pieces
_COORD = re.compile(r"\{([xyz])(-?\d*)\}")
SMALL = 12  # commands touching at most this many blocks are grouped together
GROUP = 8
SLABS = 6  # a tall solid fill goes up in at most this many pieces
MODES = ("hollow", "outline", "replace", "destroy", "keep")


def _offsets(command: str) -> list[tuple[int, int, int]]:
    values = [int(v) if v else 0 for _axis, v in _COORD.findall(command)]
    return [tuple(values[i:i + 3]) for i in range(0, len(values) - 2, 3)]  # type: ignore[misc]


def _ph(x: int, y: int, z: int) -> str:
    return f"{{x{x}}} {{y{y}}} {{z{z}}}"


def split_steps(commands: list[str], project_id: str = "") -> list[dict[str, Any]]:
    """A part's commands as pieces a girl builds one at a time: a wall goes
    up layer by layer, small blocks come a few at a time. Each piece has a
    focus (what she looks at) and a view (where she hovers), base relative."""
    pieces: list[list[str]] = []
    group: list[str] = []

    def flush() -> None:
        if group:
            pieces.append(list(group))
            group.clear()

    for command in dict.fromkeys(commands):
        words = command.split()
        coords = _offsets(command)
        if words[0] == "fill" and len(coords) >= 2:
            (x1, y1, z1), (x2, y2, z2) = coords[0], coords[1]
            block = words[7] if len(words) > 7 else ""
            mode = words[-1] if words[-1] in MODES else ""
            volume = (abs(x2 - x1) + 1) * (abs(y2 - y1) + 1) * (abs(z2 - z1) + 1)
            if volume <= SMALL:
                group.append(command)
                if len(group) >= GROUP:
                    flush()
                continue
            flush()
            if block.endswith(":air") or y1 == y2 or mode in ("replace", "keep", "destroy"):
                pieces.append([command])
                continue
            low, high = min(y1, y2), max(y1, y2)
            if mode == "hollow":  # a ring per layer (a thicker outline would also fill floors)
                for y in range(low, high + 1):
                    tail = "" if y in (low, high) else " outline"
                    pieces.append([f"fill {_ph(x1, y, z1)} {_ph(x2, y, z2)} {block}{tail}"])
                continue
            tail = f" {mode}" if mode else ""
            slab = -(-(high - low + 1) // SLABS)  # at most SLABS pieces per solid fill
            for y in range(low, high + 1, slab):
                top = min(high, y + slab - 1)
                pieces.append([f"fill {_ph(x1, y, z1)} {_ph(x2, top, z2)} {block}{tail}"])
            continue
        group.append(command)
        if len(group) >= GROUP:
            flush()
    flush()
    steps = []
    for piece in pieces:
        points = [c for cmd in piece for c in _offsets(cmd)] or [(0, 0, 0)]
        fx = sum(p[0] for p in points) / len(points)
        fy = sum(p[1] for p in points) / len(points)
        fz = sum(p[2] for p in points) / len(points)
        if project_id == "neural_net":
            # in front of the wall (never behind it), far enough back that the
            # camera through her eyes shows a good part of the network
            view = (fx - 16, max(fy + 2, 12), fz * 0.6)
        elif (fx * fx + fz * fz) ** 0.5 < 4:
            view = (fx, fy + 7, fz - 18)  # a ring around the base: from outside the gate side
        else:
            length = (fx * fx + fz * fz) ** 0.5
            view = (fx + fx / length * 7, fy + 4, fz + fz / length * 7)
        steps.append({"commands": piece, "focus": (fx, fy, fz), "view": view})
    return steps


# Blocks the girls may switch a part to (!changeMaterial): pretty, solid, safe.
MATERIALS = {
    "stone_bricks", "quartz_block", "smooth_quartz", "deepslate_bricks", "mossy_stone_bricks", "sandstone",
    "smooth_sandstone", "red_sandstone", "prismarine_bricks", "dark_prismarine", "purpur_block",
    "polished_blackstone_bricks", "bricks", "mud_bricks", "tuff_bricks", "calcite", "amethyst_block",
    "white_concrete", "pink_concrete", "light_blue_concrete", "purple_concrete", "lime_concrete",
    "yellow_concrete", "orange_concrete", "red_concrete", "black_concrete", "cyan_concrete",
    "oak_planks", "spruce_planks", "birch_planks", "cherry_planks", "dark_oak_planks", "bamboo_planks",
    "gold_block", "emerald_block", "diamond_block", "glass", "white_stained_glass", "pink_stained_glass",
}


def main_block(steps: list[dict[str, Any]]) -> str:
    """The block most of a part is made of (what !changeMaterial swaps)."""
    counts: dict[str, int] = {}
    for step in steps:
        for command in step["commands"]:
            for name in re.findall(r"minecraft:([a-z_]+)", command):
                if name != "air":
                    counts[name] = counts.get(name, 0) + 1
    return max(counts, key=counts.get) if counts else ""


# ---------------------------------------------------------------- block by block
RUN = 8  # blocks a girl lays in one go before she moves on
INSTANT = 400  # bigger fills (clearing the site, a lawn) happen in one go


def _fill(x1: int, y1: int, z1: int, x2: int, y2: int, z2: int, block: str) -> str:
    return f"fill {_ph(x1, y1, z1)} {_ph(x2, y2, z2)} {block}"


def _runs_x(x1: int, x2: int, y: int, z: int, block: str) -> list[str]:
    lo, hi = min(x1, x2), max(x1, x2)
    return [_fill(a, y, z, min(hi, a + RUN - 1), y, z, block) for a in range(lo, hi + 1, RUN)]


def _runs_z(z1: int, z2: int, y: int, x: int, block: str) -> list[str]:
    lo, hi = min(z1, z2), max(z1, z2)
    return [_fill(x, y, a, x, y, min(hi, a + RUN - 1), block) for a in range(lo, hi + 1, RUN)]


def lay(command: str) -> list[str]:
    """One build command as the short runs a girl lays one after another.
    Clearing air, lawns and other huge fills, summons and features stay whole."""
    words = command.split()
    coords = _offsets(command)
    if words[0] != "fill" or len(coords) < 2:
        return [command]
    (x1, y1, z1), (x2, y2, z2) = coords[0], coords[1]
    block = words[7] if len(words) > 7 else ""
    mode = words[-1] if words[-1] in MODES else ""
    volume = (abs(x2 - x1) + 1) * (abs(y2 - y1) + 1) * (abs(z2 - z1) + 1)
    if block.endswith(":air") or volume > INSTANT or mode in ("replace", "keep", "destroy", "hollow") or volume <= RUN:
        return [command]
    out: list[str] = []
    for y in range(min(y1, y2), max(y1, y2) + 1):
        if mode == "outline":  # a ring: its four sides
            out += _runs_x(x1, x2, y, z1, block)
            if z2 != z1:
                out += _runs_x(x1, x2, y, z2, block)
            if abs(z2 - z1) > 1:
                inner = (min(z1, z2) + 1, max(z1, z2) - 1)
                out += _runs_z(*inner, y, x1, block)
                if x2 != x1:
                    out += _runs_z(*inner, y, x2, block)
            continue
        if abs(x2 - x1) >= abs(z2 - z1):
            for z in range(min(z1, z2), max(z1, z2) + 1):
                out += _runs_x(x1, x2, y, z, block)
        else:
            for x in range(min(x1, x2), max(x1, x2) + 1):
                out += _runs_z(z1, z2, y, x, block)
    return out


HAND_LIMIT = 64  # a run with more blocks than this is not laid by hand


def hand_blocks(command: str) -> Optional[tuple[list[tuple[int, int, int]], str]]:
    """An absolute setblock or plain fill as the single blocks a girl lays by
    hand, in laying order, and the block. None for anything else (air,
    replace/keep/hollow fills, summons, NBT, too many blocks)."""
    words = command.split()
    if not words:
        return None
    try:
        if words[0] == "setblock" and len(words) in (5, 6):
            x, y, z = (int(v) for v in words[1:4])
            corners = ((x, y, z), (x, y, z))
            block, mode = words[4], (words[5] if len(words) == 6 else "replace")
            if mode != "replace":
                return None
        elif words[0] == "fill" and len(words) in (8, 9):
            a = tuple(int(v) for v in words[1:4])
            b = tuple(int(v) for v in words[4:7])
            corners = (a, b)
            block, mode = words[7], (words[8] if len(words) == 9 else "")
            if mode not in ("", "outline"):
                return None
        else:
            return None
    except ValueError:
        return None
    if "{" in block:
        return None
    (x1, y1, z1), (x2, y2, z2) = corners
    xs, ys, zs = sorted((x1, x2)), sorted((y1, y2)), sorted((z1, z2))
    volume = (xs[1] - xs[0] + 1) * (ys[1] - ys[0] + 1) * (zs[1] - zs[0] + 1)
    if volume > HAND_LIMIT:
        return None  # big clearing and huge fills just happen
    spots = []
    for y in range(ys[0], ys[1] + 1):
        for x in range(xs[0], xs[1] + 1):
            for z in range(zs[0], zs[1] + 1):
                if mode == "outline" and xs[0] < x < xs[1] and ys[0] < y < ys[1] and zs[0] < z < zs[1]:
                    continue  # outline keeps the inside as it is
                spots.append((x, y, z))
    return spots, block


# Blocks that have another item (or none) in a player's hand.
HAND_ITEMS = {"wall_torch": "torch", "redstone_wall_torch": "redstone_torch", "soul_wall_torch": "soul_torch",
              "water": "water_bucket", "lava": "lava_bucket", "fire": "flint_and_steel"}


def hand_item(block: str) -> str:
    """What she holds while laying `block` (no namespace, no block states);
    digging (air) is done with a pickaxe."""
    name = block.split("[", 1)[0].replace("minecraft:", "")
    if name in ("air", "cave_air"):
        return "diamond_pickaxe"
    if name.endswith("_wall_banner"):
        name = name.replace("_wall_banner", "_banner")
    if name.startswith("potted_"):
        name = name[len("potted_"):]
    return HAND_ITEMS.get(name, name)


def place_sound(block: str) -> str:
    """The sound a player hears when this block goes down (or is dug out)."""
    name = block.split("[", 1)[0].replace("minecraft:", "")
    if name in ("air", "cave_air"):
        return "block.gravel.break"
    for keys, sound in (
        (("glass", "ice"), "block.glass.place"),
        (("wool", "carpet"), "block.wool.place"),
        (("amethyst",), "block.amethyst_block.place"),
        (("sand", "gravel", "concrete_powder"), "block.sand.place"),
        (("planks", "log", "wood", "fence", "door", "barrel", "chest", "bookshelf", "crafting", "ladder", "sign"),
         "block.wood.place"),
        (("grass", "dirt", "leaves", "flower", "tulip", "poppy", "daisy", "orchid", "allium", "bluet", "rose",
          "lilac", "peony", "fern", "hay", "moss", "sapling", "farmland", "lily"), "block.grass.place"),
    ):
        if any(k in name for k in keys):
            return sound
    return "block.stone.place"


def center(command: str) -> tuple[float, float, float]:
    points = _offsets(command) or [(0, 0, 0)]
    return (
        sum(p[0] for p in points) / len(points),
        sum(p[1] for p in points) / len(points),
        sum(p[2] for p in points) / len(points),
    )


class ProjectTracker:
    """Progress toward the next milestone, the build when it is reached."""

    def __init__(
        self,
        names: list[str],
        rcon: Callable[..., Awaitable[Any]],
        push: Callable[[dict[str, Any]], Awaitable[None]],
        tell: Callable[[str], Awaitable[None]],
    ) -> None:
        self.names = names
        self.rcon = rcon
        self.push = push
        self.tell = tell  # a message to both girls
        self.minutes = max(0.05, float(os.environ.get("VR_MINECRAFT_MILESTONE_MINUTES", "12") or 12))
        self.state = self._load()
        self._items: dict[str, int] = {}
        self._last = time.time()
        self._announced = False
        self.creative = False  # set by the engine: the girls build piece by piece
        self._steps_key: Optional[tuple[int, int]] = None
        self._steps: list[dict[str, Any]] = []
        self._loaded = False

    # ------------------------------------------------------------ state
    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(STATE_FILE.read_text())
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {"project": 0, "milestone": 0, "progress": 0.0, "base": None}

    def _save(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(self.state, indent=2))
        except Exception as exc:
            logger.debug(f"Minecraft project not saved: {exc}")

    def current(self) -> Optional[tuple[dict[str, Any], tuple]]:
        p, m = int(self.state.get("project", 0)), int(self.state.get("milestone", 0))
        if p >= len(PROJECTS):
            return None
        project = PROJECTS[p]
        return project, project["milestones"][min(m, len(project["milestones"]) - 1)]

    def view(self) -> dict[str, Any]:
        now = self.current()
        if now is None:
            return {"title": "🛠️ Free building", "step": 'chat decides: type "build ..."', "progress": 1.0, "steps": []}
        project, milestone = now
        m = int(self.state.get("milestone", 0))
        return {
            "title": project["title"],
            "step": milestone[0],
            "progress": round(min(1.0, float(self.state.get("progress", 0.0))), 3),
            "steps": [{"name": s[0], "done": i < m} for i, s in enumerate(project["milestones"])],
        }

    # ------------------------------------------------------------ progress
    async def update(self, positions: dict[str, tuple[float, float, float]], inventories: dict[str, dict[str, int]]) -> None:
        """Called with each state update (about every second)."""
        now = time.time()
        dt = min(10.0, now - self._last)
        self._last = now
        current = self.current()
        if current is None or not positions:
            return
        if self.state.get("base") is None:
            first = next(iter(positions.values()))
            self.state["base"] = [int(first[0]), int(first[1]), int(first[2])]
            self._save()
            logger.info(f"Minecraft: the base is at {self.state['base']}")
        if not self._announced:
            self._announced = True
            await self._announce_goal()
        gained = 0
        for name, counts in inventories.items():
            for item, count in counts.items():
                key = f"{name}:{item}"
                before = self._items.get(key)
                if before is not None and count > before:
                    gained += count - before
                self._items[key] = count
        if self.creative:  # the builder loop moves the progress
            await self.push({"kind": "project", **self.view()})
            return
        # Time alone finishes a milestone in `minutes`; gathering makes it faster.
        step = dt / (self.minutes * 60) + min(gained, 20) * 0.004
        self.state["progress"] = float(self.state.get("progress", 0.0)) + step
        if self.state["progress"] >= 1.0:
            await self._complete()
        self._save()
        await self.push({"kind": "project", **self.view()})

    async def _announce_goal(self) -> None:
        current = self.current()
        if current is None:
            return
        project, (name, gather, _build) = current
        if self.creative:
            await self.tell(
                f"Your big team project right now is {project['title']}. You are building {name} piece by piece, "
                "flying around in creative. Talk about what you are building and how it should look."
            )
            return
        await self.tell(
            f"Your big team project right now is {project['title']}. Next part: {name}. "
            f"To make it happen, {gather}. Talk about it with each other and get to work."
        )

    # ------------------------------------------------------------ creative building
    def timed(self) -> bool:
        current = self.current()
        return bool(current and getattr(current[1][2], "timed", False))

    def steps(self) -> list[dict[str, Any]]:
        current = self.current()
        if current is None:
            return []
        key = (int(self.state.get("project", 0)), int(self.state.get("milestone", 0)))
        if key != self._steps_key:
            project, (_name, _gather, build) = current
            self._steps = [] if getattr(build, "timed", False) else split_steps(build(), project["id"])
            self._steps_key = key
        return self._steps

    def set_material(self, block: str) -> str:
        """A girl picked a new main block for the part she is building.
        Returns what it replaces, or "" when the block is not allowed."""
        block = block.replace("minecraft:", "").strip().lower()
        main = main_block(self.steps())
        if block not in MATERIALS or not main or main == block:
            return ""
        self.state["material"] = {"part": [self.state.get("project", 0), self.state.get("milestone", 0)],
                                  "from": main, "to": block}
        self._save()
        return main

    def _swap(self, command: str) -> str:
        swap = self.state.get("material") or {}
        part = [self.state.get("project", 0), self.state.get("milestone", 0)]
        if swap.get("part") != part:
            return command
        return re.sub(rf"minecraft:{re.escape(swap['from'])}(?![a-z_])", f"minecraft:{swap['to']}", command)

    async def ensure_loaded(self) -> bool:
        if self._loaded:
            return True
        x0, _y0, z0 = tuple(self.state["base"])
        area = f"{x0 + LOADED[0]} {z0 + LOADED[1]} {x0 + LOADED[2]} {z0 + LOADED[3]}"
        if await self.rcon(f"forceload add {area}", reply=True) is False:
            return False
        self._loaded = True
        await asyncio.sleep(2.0)
        return True

    def absolute(self, command: str) -> str:
        """A build command with the girls' material choice, in world coordinates."""
        owners = {n: offline_uuid_ints(n) for n in self.names}
        return place(self._swap(command), tuple(self.state["base"]), owners)

    async def place_one(self, command: str) -> bool:
        """One command (a run of blocks) into the world, with the girls' material choice."""
        owners = {n: offline_uuid_ints(n) for n in self.names}
        reply = await self.rcon(place(self._swap(command), tuple(self.state["base"]), owners), reply=True)
        if reply is False:
            return False
        if isinstance(reply, str) and any(w in reply for w in REFUSED) and "Could not set the block" not in reply:
            logger.warning(f"Minecraft build run refused: {reply[:160]} ({command[:80]})")
        return True

    async def run_step(self, step: dict[str, Any]) -> bool:
        """One piece into the world. False without a server console."""
        base = tuple(self.state["base"])
        owners = {n: offline_uuid_ints(n) for n in self.names}
        if not self._loaded:
            x0, _y0, z0 = base
            area = f"{x0 + LOADED[0]} {z0 + LOADED[1]} {x0 + LOADED[2]} {z0 + LOADED[3]}"
            if await self.rcon(f"forceload add {area}", reply=True) is False:
                return False
            self._loaded = True
            await asyncio.sleep(2.0)
        for command in step["commands"]:
            reply = await self.rcon(place(self._swap(command), base, owners), reply=True)
            if reply is False:
                return False
            if isinstance(reply, str) and any(w in reply for w in REFUSED) and "Could not set the block" not in reply:
                logger.warning(f"Minecraft build piece refused: {reply[:160]} ({command[:80]})")
        return True

    async def finish_timed(self) -> None:
        """The end of a timed part: its final drawing, then the next part."""
        await self._build_all()
        await self.advance()

    # ------------------------------------------------------------ survival building
    async def _complete(self) -> None:
        if await self._build_all():
            await self.advance()

    async def _build_all(self) -> bool:
        current = self.current()
        if current is None:
            return False
        project, (name, _gather, build) = current
        base = tuple(self.state["base"])
        owners = {n: offline_uuid_ints(n) for n in self.names}
        x0, y0, z0 = base
        # Nobody may stand where blocks appear (a bot inside a block gets
        # kicked): each girl goes to a spot outside everything first.
        # (spreadplayers takes one target: two names in one command failed.)
        if not self.creative:
            for girl in self.names:
                await self.rcon(f"spreadplayers {x0 + VIEW_SPOT[0]} {z0 + VIEW_SPOT[1]} 0 3 false {girl}", reply=True)
        # Every build stays inside this box; keep its chunks loaded while
        # building (parts far from the girls were "not loaded" and skipped).
        area = f"{x0 + LOADED[0]} {z0 + LOADED[1]} {x0 + LOADED[2]} {z0 + LOADED[3]}"
        await self.rcon(f"forceload add {area}", reply=True)
        await asyncio.sleep(2.0)
        built = 0
        try:
            for command in dict.fromkeys(build()):  # lines that cross share blocks: once is enough
                reply = await self.rcon(place(command, base, owners), reply=True)
                if reply is False:
                    logger.warning("Minecraft: no server console, the project part was not built (restart the stream)")
                    self.state["progress"] = 0.95
                    return False
                # "Could not set the block" only means it is already that block.
                if isinstance(reply, str) and any(w in reply for w in REFUSED) and "Could not set the block" not in reply:
                    logger.warning(f"Minecraft build step refused: {reply[:160]} ({command[:80]})")
                built += 1
        finally:
            if not (self.creative and self._loaded):
                await self.rcon(f"forceload remove {area}", reply=True)
        logger.info(f"Minecraft: {project['title']}: {name} built ({built} commands)")
        return True

    async def advance(self) -> None:
        """This part is done: celebrate, and the next part starts."""
        current = self.current()
        if current is None:
            return
        project, (name, _gather, _build) = current
        await self.push({"kind": "project_done", "title": project["title"], "step": name})
        from ..live.clip_marks import CLIPS

        p_next = int(self.state["milestone"]) + 1 >= len(project["milestones"])
        CLIPS.mark("built", f"{project['title']} COMPLETE" if p_next else f"{name} finished ({project['title']})",
                   4.0 if p_next else 3.0)
        p, m = int(self.state["project"]), int(self.state["milestone"]) + 1
        finished_project = m >= len(project["milestones"])
        if finished_project:
            p, m = p + 1, 0
        self.state.update({"project": p, "milestone": m, "progress": 0.0, "built": 0})
        self._save()
        nxt = self.current()
        if finished_project:
            text = f"{project['title']} is COMPLETE! Go look at it and celebrate loudly together!"
        else:
            text = f"{name} for {project['title']} is finished! Go look at it and celebrate!"
        if nxt is not None:
            if self.creative:
                text += f" Next you build: {nxt[1][0]} for {nxt[0]['title']}. Talk about how it should look."
            else:
                text += f" Next up: {nxt[1][0]} for {nxt[0]['title']}. To make it happen, {nxt[1][1]}. Get to work on it."
        await self.tell(text)
