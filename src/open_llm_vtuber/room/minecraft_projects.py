"""The long goals of the Minecraft show: a castle that grows, then a green
farm, a flower garden and pets.

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

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

STATE_FILE = Path("data/minecraft_project.json")
VIEW_SPOT = (24, -4)  # x, z from the base: outside every build, with a view of the castle

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
            return {"title": "🌟 Everything is built!", "step": "Free play", "progress": 1.0, "steps": []}
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
        await self.tell(
            f"Your big team project right now is {project['title']}. Next part: {name}. "
            f"To make it happen, {gather}. Talk about it with each other and get to work."
        )

    async def _complete(self) -> None:
        current = self.current()
        if current is None:
            return
        project, (name, _gather, build) = current
        base = tuple(self.state["base"])
        owners = {n: offline_uuid_ints(n) for n in self.names}
        x0, y0, z0 = base
        # Nobody may stand where blocks appear (a bot inside a block gets
        # kicked): both go to a spot outside everything first.
        names = " ".join(self.names)
        await self.rcon(f"spreadplayers {x0 + VIEW_SPOT[0]} {z0 + VIEW_SPOT[1]} 1 3 false {names}", reply=True)
        built = 0
        for command in build():
            reply = await self.rcon(place(command, base, owners), reply=True)
            if reply is False:
                logger.warning("Minecraft: no server console, the project part was not built (restart the stream)")
                self.state["progress"] = 0.95
                return
            if isinstance(reply, str) and any(w in reply for w in ("Unknown", "Incorrect", "Invalid", "Expected", "error", "Could not")):
                logger.warning(f"Minecraft build step refused: {reply[:160]} ({command[:80]})")
            built += 1
        logger.info(f"Minecraft: {project['title']}: {name} built ({built} commands)")

        await self.push({"kind": "project_done", "title": project["title"], "step": name})
        p, m = int(self.state["project"]), int(self.state["milestone"]) + 1
        finished_project = m >= len(project["milestones"])
        if finished_project:
            p, m = p + 1, 0
        self.state.update({"project": p, "milestone": m, "progress": 0.0})
        self._save()
        nxt = self.current()
        if finished_project:
            text = f"{project['title']} is COMPLETE! Go look at it and celebrate loudly together!"
        else:
            text = f"{name} for {project['title']} is finished! Go look at it and celebrate!"
        if nxt is not None:
            text += f" Next up: {nxt[1][0]} for {nxt[0]['title']}. To make it happen, {nxt[1][1]}. Get to work on it."
        await self.tell(text)
