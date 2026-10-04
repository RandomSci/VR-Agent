"""Free building: whatever chat (or a girl) asks for, built BY HAND.

"build a sky castle with my name Selwyn on it", "place a block of stone on
the sand": one small AI call designs it as a few boxes (and letters), and the
design becomes short runs of blocks that both girls lay by hand, one block
per arm swing, through the same code as the big projects (_lay_runs).

A design is relative to the spot in front of the camera girl: x left/right,
y up from the ground, z away from her. It is turned to the nearest compass
direction she faces, so the build always appears in front of the camera."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from loguru import logger

from .minecraft_templates import DEFAULT_COLOURS, SIZES, TEMPLATES, footprint
from .minecraft_templates import build as make_template

# Only what would wreck the stream: fire and lava spread and burn the builds,
# command and structure blocks control the server. Everything else is fine.
DESIGN_BANNED = re.compile(r"(command_block|structure_block|structure_void|jigsaw|lava|fire|barrier|spawner)")
# Blocks that bring a build to life: a carved pumpkin on snow blocks is a
# snow golem (ten snowmen walked away as golems), on iron blocks an iron
# golem, wither skulls on soul sand the Wither. The plain pumpkin and skull
# look the same and stay blocks.
COMES_ALIVE = {
    "carved_pumpkin": "pumpkin", "jack_o_lantern": "pumpkin",
    "wither_skeleton_skull": "skeleton_skull", "wither_skeleton_wall_skull": "skeleton_wall_skull",
}
# Big on purpose: chat may ask for anything, and it is built (by hand).
SIZE_X = 40  # x from -40 to 40
SIZE_Y = 80
DIG_Y = -30  # holes go this deep at most
SIZE_Z = 80
MAX_PARTS = 150
MAX_BLOCKS = 20000  # a free design (a huge one takes an hour by hand: fine on an 11 hour stream)
MAX_TEMPLATE_BLOCKS = 20000
LAYER_RUNS = 40  # blocks per piece (one piece = one hop to a new spot)
BLOCK_ID = re.compile(r"^[a-z0-9_]{2,40}$")
FALLBACK_BLOCK = "stone_bricks"

DESIGN_SYSTEM = (
    "You plan ONE Minecraft build that two players lay block by block, live on stream. "
    "Snowmen, golems and statues are made of blocks (snow blocks and a plain pumpkin head), never mobs. "
    "FIRST choose a template when it fits: garden (any garden, park, flower field), house (house, cottage, hut, "
    "home, shop), tower (tower, lighthouse, watchtower), castle (castle, fort, palace), dragon (any dragon, "
    "wyvern, giant dragon statue: large when they say giant or huge). Then answer ONLY JSON: "
    '{"title": "short name", "template": "garden", "size": "small" or "medium" or "large", "main": "block id for '
    'walls", "accent": "block id for floors, roofs, details", "sky": true when it should float (sky, flying, '
    'cloud), "text": {"words": "SELWYN", "block": "gold_block"} or null}. '
    "The template adds all the details (paths, flowers, windows, roofs, battlements, lanterns): just pick nice, "
    "matching blocks. Use template \"custom\" only when nothing fits, and then also give parts as below, with real "
    "detail (a statue has a head, arms and colours; a bridge has railings and lanterns), at least 80 blocks unless "
    "they asked for one or two blocks. Big requests may be really big: there is no limit, the stream is long. "
    "Custom parts: "
    f"Coordinates are relative: x is left (-) / right (+) from -{SIZE_X} to {SIZE_X}, y is up from the ground "
    f"(0 = the first layer of air above the ground, -1 = the ground itself) from {DIG_Y} up to {SIZE_Y}, z is "
    f"forward, away from the builders, from 0 to {SIZE_Z}. "
    '"parts": [{"from": [x, y, z], "to": [x, y, z], "block": "stone_bricks", "shape": "solid" or "walls"}]. '
    f"Rules: at most {MAX_PARTS} parts, ordered bottom to top; real Minecraft block ids only (no tnt, lava, fire, "
    'command blocks); "walls" is a hollow box with four sides (rooms, towers, keeps), "solid" is filled (floors, '
    f"platforms, roofs, pillars); fewer than {MAX_BLOCKS} blocks in all. A small request stays small: one block is "
    "one part. To DIG (a hole, a pit, a tunnel, a moat, a cave) use block \"air\" with y below 0, for example a "
    "hole is from [-1, -4, 3] to [1, -1, 5]. "
    "Floating or sky things start at y 10 or higher on a solid platform. When the viewer wants words or "
    "their name on it, use text (capital letters A to Z and digits, at most 10 characters); the words stand on the "
    "front (z of the front wall) facing the builders, at a height on the build."
)

# A tiny 3x5 pixel font: each letter is 5 rows of 3 pixels ("#" = a block).
FONT = {
    "A": ("###", "#.#", "###", "#.#", "#.#"), "B": ("##.", "#.#", "##.", "#.#", "##."),
    "C": ("###", "#..", "#..", "#..", "###"), "D": ("##.", "#.#", "#.#", "#.#", "##."),
    "E": ("###", "#..", "##.", "#..", "###"), "F": ("###", "#..", "##.", "#..", "#.."),
    "G": ("###", "#..", "#.#", "#.#", "###"), "H": ("#.#", "#.#", "###", "#.#", "#.#"),
    "I": ("###", ".#.", ".#.", ".#.", "###"), "J": ("..#", "..#", "..#", "#.#", "###"),
    "K": ("#.#", "#.#", "##.", "#.#", "#.#"), "L": ("#..", "#..", "#..", "#..", "###"),
    "M": ("#.#", "###", "###", "#.#", "#.#"), "N": ("##.", "#.#", "#.#", "#.#", "#.#"),
    "O": ("###", "#.#", "#.#", "#.#", "###"), "P": ("###", "#.#", "###", "#..", "#.."),
    "Q": ("###", "#.#", "#.#", "###", "..#"), "R": ("##.", "#.#", "##.", "#.#", "#.#"),
    "S": ("###", "#..", "###", "..#", "###"), "T": ("###", ".#.", ".#.", ".#.", ".#."),
    "U": ("#.#", "#.#", "#.#", "#.#", "###"), "V": ("#.#", "#.#", "#.#", "#.#", ".#."),
    "W": ("#.#", "#.#", "###", "###", "#.#"), "X": ("#.#", "#.#", ".#.", "#.#", "#.#"),
    "Y": ("#.#", "#.#", ".#.", ".#.", ".#."), "Z": ("###", "..#", ".#.", "#..", "###"),
    "0": ("###", "#.#", "#.#", "#.#", "###"), "1": (".#.", "##.", ".#.", ".#.", "###"),
    "2": ("###", "..#", "###", "#..", "###"), "3": ("###", "..#", "###", "..#", "###"),
    "4": ("#.#", "#.#", "###", "..#", "..#"), "5": ("###", "#..", "###", "..#", "###"),
    "6": ("###", "#..", "###", "#.#", "###"), "7": ("###", "..#", "..#", "..#", "..#"),
    "8": ("###", "#.#", "###", "#.#", "###"), "9": ("###", "#.#", "###", "..#", "###"),
    " ": ("...", "...", "...", "...", "..."),
}


def _clamp(v: Any, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(round(float(v)))))
    except (TypeError, ValueError):
        return lo


def block_id(name: Any) -> str:
    """A safe, plain block id (no states, nothing that breaks the stream)."""
    text = str(name or "").lower().replace("minecraft:", "").strip()
    text = text.split("[", 1)[0].split("{", 1)[0]
    if text in ("air", "cave_air"):
        return "air"  # digging
    if not BLOCK_ID.match(text) or DESIGN_BANNED.search(text):
        return FALLBACK_BLOCK
    return COMES_ALIVE.get(text, text)


def parse_design(raw: str) -> Optional[dict[str, Any]]:
    """The AI's JSON, checked and clamped. None when it is unusable."""
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    parts = []
    for part in (data.get("parts") or [])[:MAX_PARTS]:
        if not isinstance(part, dict):
            continue
        a, b = part.get("from") or [], part.get("to") or part.get("from") or []
        if len(a) < 3 or len(b) < 3:
            continue
        lo = [_clamp(a[0], -SIZE_X, SIZE_X), _clamp(a[1], DIG_Y, SIZE_Y), _clamp(a[2], 0, SIZE_Z)]
        hi = [_clamp(b[0], -SIZE_X, SIZE_X), _clamp(b[1], DIG_Y, SIZE_Y), _clamp(b[2], 0, SIZE_Z)]
        lo, hi = [min(p, q) for p, q in zip(lo, hi)], [max(p, q) for p, q in zip(lo, hi)]
        shape = "walls" if str(part.get("shape", "")).lower().startswith(("wall", "hollow", "frame")) else "solid"
        parts.append({"from": lo, "to": hi, "block": block_id(part.get("block")), "shape": shape})
    text = data.get("text") if isinstance(data.get("text"), dict) else None
    words = re.sub(r"[^A-Z0-9 ]", "", str((text or {}).get("words", "")).upper())[:10].strip()
    template = str(data.get("template") or "").lower().strip()
    if template not in TEMPLATES:
        template = ""
    if not parts and not words and not template:
        return None
    main, accent = DEFAULT_COLOURS.get(template, ("stone_bricks", "polished_andesite"))
    return {
        "title": str(data.get("title") or "A build for chat")[:40],
        "template": template,
        "size": str(data.get("size") or "medium").lower() if str(data.get("size") or "").lower() in SIZES else "medium",
        "main": block_id(data.get("main")) if data.get("main") and block_id(data.get("main")) != "air" else main,
        "accent": block_id(data.get("accent")) if data.get("accent") and block_id(data.get("accent")) != "air" else accent,
        "sky": bool(data.get("sky")),
        "parts": parts if not template else [],
        "text": {
            "words": words,
            "block": block_id((text or {}).get("block") or "gold_block"),
            "y": _clamp((text or {}).get("y", 2), 0, SIZE_Y - 5),
            "z": _clamp((text or {}).get("z", 0), 0, SIZE_Z),
        } if words else None,
    }


def blocks_of(design: dict[str, Any]) -> list[tuple[tuple[int, int, int], str]]:
    """Every block of the design, relative, in laying order (digging top
    down first, then building bottom up), each spot once."""
    out: dict[tuple[int, int, int], str] = {}
    if design.get("template"):
        out.update(make_template(design["template"], design.get("size", "medium"), design.get("main", "stone_bricks"),
                                 design.get("accent", "polished_andesite"), sky=design.get("sky", False),
                                 seed=sum(map(ord, design.get("title", "")))))
    for part in design.get("parts") or []:
        (x1, y1, z1), (x2, y2, z2) = part["from"], part["to"]
        for y in range(y1, y2 + 1):
            for z in range(z1, z2 + 1):
                for x in range(x1, x2 + 1):
                    if part["shape"] == "walls" and x1 < x < x2 and z1 < z < z2:
                        continue  # a hollow room: only its four sides
                    out[(x, y, z)] = part["block"]
    text = design.get("text")
    if text and design.get("template") and out:
        # the name where everyone sees it: above the front of a tall build,
        # standing behind a low one
        x1, x2, z1, z2, top = footprint(out)
        text = dict(text, y=top + 2, z=z1) if top > 8 else dict(text, y=1, z=z2 + 3)
    if text:
        words = text["words"]
        width = len(words) * 4 - 1
        left = -(width // 2)
        for i, ch in enumerate(words):
            rows = FONT.get(ch, FONT[" "])
            for r, row in enumerate(rows):
                for c, pixel in enumerate(row):
                    if pixel == "#":
                        # letters face the builders: reading left to right from where they stand
                        out[(left + i * 4 + c, text["y"] + 4 - r, text["z"] - 1)] = text["block"]
    return ordered(out)[:MAX_TEMPLATE_BLOCKS if design.get("template") else MAX_BLOCKS]


def ordered(out: dict[tuple[int, int, int], str]) -> list[tuple[tuple[int, int, int], str]]:
    """Laying order: digging top down first, then building bottom up."""
    digs = sorted(((p, b) for p, b in out.items() if b == "air"), key=lambda kv: (-kv[0][1], kv[0][2], kv[0][0]))
    builds = sorted(((p, b) for p, b in out.items() if b != "air"), key=lambda kv: (kv[0][1], kv[0][2], kv[0][0]))
    return digs + builds


def footprint_of(spots: dict[tuple[int, int, int], str]) -> tuple[int, int, int, int, int]:
    return footprint(spots)


def to_world(rel: tuple[int, int, int], forward: tuple[int, int]) -> tuple[int, int, int]:
    """Relative (x right, y up, z forward) -> world offsets for a builder facing `forward`."""
    fx, fz = forward
    rx, rz = -fz, fx  # her right hand: facing +z (south) means right is -x (west)
    x, y, z = rel
    return x * rx + z * fx, y, x * rz + z * fz


def _lines(blocks: list[tuple[tuple[int, int, int], str]]) -> list[str]:
    """Neighbouring blocks in a row become one line (laid in one go, by hand)."""
    left = dict(blocks)
    out = []
    for (x, y, z), block in blocks:
        if (x, y, z) not in left:
            continue
        for dx, dz in ((1, 0), (0, 1)):
            end = 0
            while left.get((x + dx * (end + 1), y, z + dz * (end + 1))) == block:
                end += 1
            if end:
                break
        for i in range(end + 1):
            left.pop((x + dx * i, y, z + dz * i), None)
        if end:
            out.append(f"fill {{x{x}}} {{y{y}}} {{z{z}}} {{x{x + dx * end}}} {{y{y}}} {{z{z + dz * end}}} minecraft:{block}")
        else:
            out.append(f"setblock {{x{x}}} {{y{y}}} {{z{z}}} minecraft:{block}")
    return out


def cardinal(dx: float, dz: float) -> tuple[int, int]:
    if abs(dx) >= abs(dz):
        return (1 if dx >= 0 else -1, 0)
    return (0, 1 if dz >= 0 else -1)


def pieces(design: dict[str, Any], origin: tuple[int, int, int], base: tuple[int, int, int],
           forward: tuple[int, int]) -> list[dict[str, Any]]:
    return pieces_of(blocks_of(design), origin, base, forward)


def pieces_of(blocks: list[tuple[tuple[int, int, int], str]], origin: tuple[int, int, int],
              base: tuple[int, int, int], forward: tuple[int, int]) -> list[dict[str, Any]]:
    """Blocks (in laying order) as build steps for _lay_runs: commands with the
    base placeholders, LAYER_RUNS blocks per piece, a focus and a view each."""
    ox, oy, oz = origin
    bx, by, bz = base
    steps: list[dict[str, Any]] = []
    group: list[tuple[tuple[int, int, int], str]] = []

    def flush() -> None:
        if not group:
            return
        world = []
        for rel, block in group:
            wx, wy, wz = to_world(rel, forward)
            world.append(((ox + wx - bx, oy + wy - by, oz + wz - bz), block))
        commands = _lines(world)
        world = [p for p, _b in world]
        fx = sum(p[0] for p in world) / len(world)
        fy = sum(p[1] for p in world) / len(world)
        fz = sum(p[2] for p in world) / len(world)
        # she hovers on the builders' side, a little above, looking at it
        view = (fx - forward[0] * 5, fy + 3, fz - forward[1] * 5)
        steps.append({"commands": commands, "focus": (fx, fy, fz), "view": view})
        group.clear()

    last_y = None
    for rel, block in blocks:
        if group and (len(group) >= LAYER_RUNS or rel[1] != last_y):
            flush()
        group.append((rel, block))
        last_y = rel[1]
    flush()
    return steps


async def design(llm: Any, model: str, request: str, who: str) -> Optional[dict[str, Any]]:
    """One AI call: the request as a design (None without an API key or on errors)."""
    if llm is None or not (os.environ.get("OPENAI_API_KEY") or "").strip():
        return None
    try:
        # Its own time limit: the shared client allows chat answers 8 s, and a
        # big custom design takes longer (they all failed after being promised).
        if hasattr(llm, "with_options"):
            llm = llm.with_options(timeout=DESIGN_TIMEOUT, max_retries=0)
        response = await llm.chat.completions.create(
            model=os.environ.get("VR_MINECRAFT_DESIGN_MODEL", "").strip() or DESIGN_MODEL,
            messages=[
                {"role": "system", "content": DESIGN_SYSTEM},
                {"role": "user", "content": f"{who} asked: {request}"},
            ],
            max_tokens=4000,
            temperature=0.6,
            response_format={"type": "json_object"},
        )
        return parse_design(response.choices[0].message.content or "")
    except Exception as exc:  # pragma: no cover - network dependent
        logger.warning(f"Minecraft: the build could not be designed: {exc}")
        return None


DESIGN_TIMEOUT = 60.0
DESIGN_MODEL = "gpt-4o"  # one call per build (about 1 cent): much better designs than the mini model
BUILD_ASK = re.compile(r"\b(build|place|put|construct|dig|make (?:me |us )?an?)\b", re.I)


def wants_build(text: str) -> bool:
    """'build a sky castle', 'Mika create a pumpkin' (a request, not 'first place!')."""
    from .minecraft_asks import wants_build as asked

    return asked(text)
