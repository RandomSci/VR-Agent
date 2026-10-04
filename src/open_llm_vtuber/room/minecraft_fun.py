"""The fun on top of building: anything from the creative inventory, potions
(drinking, splashing each other, mystery brews), a flying race through sky
hoops, and "stand in front" so the camera (Mika's eyes) sees her friend.

Everything visible happens in the real game through the server console
(RCON): effects with their particles and sounds, titles on screen for the
countdown, fireworks for the winner. The girls only decide; the engine makes
it happen, and each girl's bot is told the result so she can react."""

from __future__ import annotations

import asyncio
import math
import random
import re
import time
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

from ..live.clip_marks import CLIPS

if TYPE_CHECKING:  # pragma: no cover
    from .minecraft_mode import MinecraftEngine

# Effects that look fun on stream and hurt nobody. Seconds, level (0 = I).
EFFECTS = {
    "speed": (25, 1), "slowness": (15, 1), "jump_boost": (25, 1), "levitation": (6, 0),
    "slow_falling": (20, 0), "glowing": (30, 0), "invisibility": (20, 0), "night_vision": (30, 0),
    "regeneration": (15, 0), "strength": (25, 0), "nausea": (10, 0), "haste": (25, 1),
}
ALIASES = {
    "swiftness": "speed", "fast": "speed", "jump": "jump_boost", "leaping": "jump_boost", "float": "levitation",
    "floating": "levitation", "fly": "levitation", "glow": "glowing", "invisible": "invisibility",
    "slow": "slowness", "dizzy": "nausea", "healing": "regeneration", "heal": "regeneration",
    "feather": "slow_falling", "falling": "slow_falling", "strong": "strength",
}
MYSTERY = ["levitation", "glowing", "invisibility", "jump_boost", "speed", "slowness", "nausea", "slow_falling"]
# Never handed out: only what wrecks the stream (fire, lava, explosions
# by accident, server control). TNT blocks are fine: !blowUp lights them.
NO_ITEMS = re.compile(
    r"(command_block|structure_block|structure_void|jigsaw|barrier|^light$|debug_stick|lava|fire_charge|"
    r"flint_and_steel|spawner|end_crystal|respawn_anchor|knowledge_book)"
)
ITEM_NAME = re.compile(r"^[a-z0-9_]{2,40}$")
# The race course, base relative: straight along x, high in the sky, inside
# the area the server keeps loaded. Hoops are 9 wide (z) and 7 tall (y).
COURSE_Y = 40
COURSE_Z = 28
COURSE_X = (-20, 46)
HOOPS = ((-20, "lime_concrete"), (-4, "gold_block"), (12, "gold_block"), (28, "gold_block"), (46, "red_concrete"))
TNT_RANGE = (-200, 320)  # base relative: the blast range, away from the Kingdom, the plots and the base
TNT_SAFE = 40  # TNT only this far (blocks) from the base, never near the builds
TNT_GAP = 30.0  # seconds between two TNT
FLY_SPEED = 18.0  # blocks a second at normal pace (the !flyTo patch)
SPEED_STEP = 0.35  # each Speed level adds this much (the !flyTo patch)


def CHOICE_HOLD() -> float:  # noqa: N802 (the engine's constant, read late: no import loop)
    from .minecraft_mode import CHOICE_HOLD as hold

    return hold


def effect_name(text: str) -> str:
    name = re.sub(r"[^a-z_]", "", str(text).lower().replace(" ", "_").replace("minecraft:", ""))
    name = re.sub(r"^(potion_of_|splash_)", "", name)
    return ALIASES.get(name, name)


def hoop(x: int, block: str) -> list[str]:
    """One vertical ring across the course (base-relative fill commands)."""
    y1, y2 = COURSE_Y - 3, COURSE_Y + 3
    z1, z2 = COURSE_Z - 4, COURSE_Z + 4

    def fill(a: tuple[int, int, int], b: tuple[int, int, int]) -> str:
        return f"fill {{x{a[0]}}} {{y{a[1]}}} {{z{a[2]}}} {{x{b[0]}}} {{y{b[1]}}} {{z{b[2]}}} minecraft:{block}"

    return [
        fill((x, y1, z1), (x, y1, z2)), fill((x, y2, z1), (x, y2, z2)),
        fill((x, y1, z1), (x, y2, z1)), fill((x, y1, z2), (x, y2, z2)),
    ]


def finish_seconds(level: int, distance: float) -> float:
    """How long the !flyTo patch takes for `distance` at this Speed level (0: none)."""
    return distance / (FLY_SPEED * (1 + SPEED_STEP * level))


class FunShow:
    def __init__(self, engine: "MinecraftEngine") -> None:
        self.e = engine
        self.racing = False
        self.course_built = False
        self.levels: dict[str, int] = {}  # Speed level from potions (the race keeps them)
        self._tnt_at = 0.0

    # ------------------------------------------------------------ helpers
    async def rcon(self, command: str, reply: bool = False) -> Any:
        from . import minecraft_mode

        return await minecraft_mode.rcon_command(command, reply=reply)

    async def tell(self, cid: str, text: str) -> None:
        await self.e.link.emit("send-message", self.e.names[cid], {"from": "system", "message": text})

    def remember(self, text: str) -> None:
        self.e._remember(text)

    # ------------------------------------------------------------ items
    async def get_item(self, cid: str, item: str, count: str) -> None:
        name = str(item).lower().replace("minecraft:", "").strip()
        try:
            n = max(1, min(64, int(float(count))))
        except ValueError:
            n = 1
        if not ITEM_NAME.match(name) or NO_ITEMS.search(name):
            await self.tell(cid, f"{name} is not allowed on stream. Pick something else.")
            return
        reply = await self.rcon(f"give {self.e.names[cid]} minecraft:{name} {n}", reply=True)
        if isinstance(reply, str) and ("Unknown item" in reply or "Expected" in reply or "Invalid" in reply):
            await self.tell(cid, f"There is no item called {name} in Minecraft. Try its real name.")
            return
        self.remember(f"{self.e.names[cid]} got {n} {name}")
        await self.tell(cid, f"You have {n} {name} now.")

    # ------------------------------------------------------------ potions
    async def drink(self, cid: str, effect: str, mystery: bool = False) -> None:
        name = effect_name(effect)
        if name not in EFFECTS:
            await self.tell(cid, f"No potion of {effect} here. Pick one of: {', '.join(EFFECTS)}.")
            return
        who = self.e.names[cid]
        await self.rcon(f"item replace entity {who} weapon.mainhand with minecraft:potion")
        await self.rcon(f"execute at {who} run playsound minecraft:entity.generic.drink player @a ~ ~ ~ 1 1")
        await self.apply(cid, name)
        what = "a mystery potion" if mystery else f"a potion of {name.replace('_', ' ')}"
        self.remember(f"{who} drank {what}" + (f": it was {name.replace('_', ' ')}" if mystery else ""))
        CLIPS.mark("potion", f"{who} drank {what} ({name})", 1.5 if mystery else 1.0)
        await self.tell(cid, f"You drank {what}. It is {name.replace('_', ' ')}! React to how it feels, out loud.")

    async def splash(self, cid: str, effect: str, at: str = "") -> None:
        name = effect_name(effect)
        if name not in EFFECTS:
            await self.tell(cid, f"No splash potion of {effect}. Pick one of: {', '.join(EFFECTS)}.")
            return
        if at:  # "splash that chicken": at the nearest one of them
            await self._splash_creature(cid, name, at)
            return
        friend = self.e._friend(cid)
        if friend == cid:
            return
        who, target = self.e.names[cid], self.e.names[friend]
        # A real throw: the potion in her hand, she turns to her friend, swings
        # and throws it (it used to just happen, nothing flew).
        seconds, level = EFFECTS[name]
        await self.rcon(
            f"item replace entity {who} weapon.mainhand with minecraft:splash_potion"
            f'[minecraft:potion_contents={{custom_effects:[{{id:"minecraft:{name}",duration:{seconds * 20},'
            f"amplifier:{level}}}]}}]")
        to = self.e._pos.get(friend)
        if to and cid in self.e._pos:
            self.e._chose_at[cid] = time.time() + 6 - CHOICE_HOLD()
            await asyncio.sleep(0.3)  # the potion reaches her hand
            await self.e._command(cid, f"!gesture({to[0]:.1f}, {to[1] + 1.2:.1f}, {to[2]:.1f}, 1)")
            await asyncio.sleep(1.4)  # it flies and breaks
        await self.rcon(f"execute at {target} run particle minecraft:splash ~ ~1 ~ 0.6 0.6 0.6 0.2 60")
        await self.rcon(f"execute at {target} run playsound minecraft:entity.splash_potion.break player @a ~ ~ ~ 1 1")
        await self.apply(friend, name)  # also when the throw missed: chat asked for it
        self.remember(f"{who} splashed {target} with {name.replace('_', ' ')}")
        CLIPS.mark("potion", f"{who} splashed {target} with {name}", 1.5)
        await self.tell(friend, f"{who} just hit you with a splash potion of {name.replace('_', ' ')}! React!")

    async def _splash_creature(self, cid: str, name: str, kind: str) -> None:
        from .minecraft_mode import _numbers

        who = self.e.names[cid]
        sel = f"@e[type=minecraft:{kind},sort=nearest,limit=1,distance=..32]"
        pos = _numbers(await self.rcon(f"execute at {who} run data get entity {sel} Pos", reply=True))
        if len(pos) < 3:
            await self.tell(cid, f"There is no {kind.replace('_', ' ')} close enough to splash. Say so, funny.")
            return
        seconds, level = EFFECTS[name]
        await self.rcon(
            f"item replace entity {who} weapon.mainhand with minecraft:splash_potion"
            f'[minecraft:potion_contents={{custom_effects:[{{id:"minecraft:{name}",duration:{seconds * 20},'
            f"amplifier:{level}}}]}}]")
        self.e._chose_at[cid] = time.time() + 6 - CHOICE_HOLD()
        await asyncio.sleep(0.3)
        await self.e._command(cid, f"!gesture({pos[0]:.1f}, {pos[1] + 0.6:.1f}, {pos[2]:.1f}, 1)")
        await asyncio.sleep(1.4)
        at = f"@e[type=minecraft:{kind},sort=nearest,limit=1,x={pos[0]:.1f},y={pos[1]:.1f},z={pos[2]:.1f},distance=..4]"
        await self.rcon(f"effect give {at} minecraft:{name} {seconds} {level}")  # also when the throw missed
        await self.rcon(f"particle minecraft:splash {pos[0]:.1f} {pos[1] + 0.5:.1f} {pos[2]:.1f} 0.5 0.5 0.5 0.2 50")
        self.remember(f"{who} splashed a {kind.replace('_', ' ')} with {name.replace('_', ' ')}")
        CLIPS.mark("potion", f"{who} splashed a {kind} with {name}", 1.5)
        await self.tell(cid, f"You hit the {kind.replace('_', ' ')} with a splash potion of {name.replace('_', ' ')}! "
                             "React to what happens to it, out loud.")

    async def brew(self, cid: str) -> None:
        """A mystery brew: a brewing stand appears next to her, it bubbles, she drinks."""
        pos = self.e._pos.get(cid)
        stand = None
        if pos:
            stand = (math.floor(pos[0]) + 1, math.floor(pos[1]), math.floor(pos[2]))
            await self.rcon(f"setblock {stand[0]} {stand[1]} {stand[2]} minecraft:brewing_stand keep")
            await self.rcon(f"playsound minecraft:block.brewing_stand.brew block @a {stand[0]} {stand[1]} {stand[2]} 1 1")
            await self.rcon(f"particle minecraft:witch {stand[0] + 0.5} {stand[1] + 1} {stand[2] + 0.5} 0.3 0.4 0.3 0.05 30")
        await asyncio.sleep(2.5)
        await self.drink(cid, random.choice(MYSTERY), mystery=True)
        if stand:
            await asyncio.sleep(8)
            await self.rcon(f"setblock {stand[0]} {stand[1]} {stand[2]} minecraft:air replace minecraft:brewing_stand")

    async def apply(self, cid: str, name: str) -> None:
        seconds, level = EFFECTS[name]
        await self.rcon(f"effect give {self.e.names[cid]} minecraft:{name} {seconds} {level}")
        await self.e._push({"kind": "effect", "who": cid, "name": name.replace("_", " "), "seconds": seconds})
        if name == "speed":
            self.levels[cid] = level + 1
            asyncio.get_running_loop().call_later(seconds, self.levels.pop, cid, None)

    # ------------------------------------------------------------ in front of the camera
    async def stand_in_front(self, cid: str) -> None:
        """The girl who is not the camera flies 5 blocks in front of the camera
        girl's eyes, facing her (she flies there, around blocks: no teleport)."""
        eyes = self.e.cam_focus if self.e.cam_focus in self.e.cast else self.e.cast[0]
        mover = self.e._friend(eyes) if cid == eyes else cid
        if mover == eyes or eyes not in self.e._pos:
            return
        reply = await self.rcon(f"data get entity {self.e.names[eyes]} Rotation", reply=True)
        numbers = re.findall(r"-?\d+(?:\.\d+)?", str(reply).split(":")[-1]) if isinstance(reply, str) else []
        fx, fz = self.e._facing(eyes, math.radians(float(numbers[0])) if numbers else None)
        ex, ey, ez = self.e._pos[eyes]
        x, z = ex + fx * 5.0, ez + fz * 5.0
        self.e._target[mover] = (x, ey, z)
        self.e._chose_at[mover] = time.time()
        await self.e._command(mover, f"!flyTo({x:.1f}, {ey:.1f}, {z:.1f}, {ex:.1f}, {ey + 1.6:.1f}, {ez:.1f}, 0)")
        self.remember(f"{self.e.names[mover]} flew in front of {self.e.names[eyes]}")

    # ------------------------------------------------------------ TNT
    async def tnt(self, cid: str) -> None:
        """She places TNT by hand in front of the camera, it is lit, it really
        explodes. Never near the builds (castle, network...): the stream would
        lose hours of work in one second."""
        now = time.time()
        if now - self._tnt_at < TNT_GAP:
            await self.tell(cid, "The last TNT just went off. Wait a little before the next one.")
            return
        base = self.e.projects.state.get("base")
        eyes = self.e.cam_focus if self.e.cam_focus in self.e.cast else self.e.cast[0]
        who = cid if cid in self.e._pos else eyes
        if not base or who not in self.e._pos:
            return
        x0, y0, z0 = self.e._pos[who]
        fx, fz = self.e._facing(who)
        x, y, z = math.floor(x0 + fx * 8), math.floor(y0), math.floor(z0 + fz * 8)
        bx, by, bz = base
        self._tnt_at = now
        if math.hypot(x - bx, z - bz) < TNT_SAFE or self.e.near_builds(x, z, 10):
            # In front of her is something they built (the Kingdom is huge, and
            # the TNT used to land in the lot being built): she flies to the
            # blast range first, where nothing is built, and blows it up there.
            self.e._chose_at[who] = time.time() + 40
            rx, rz = TNT_RANGE
            ground = await self.e_ground(bx + rx, bz + rz, by)
            view, look = (rx, ground - by + 3, rz - 9), (rx, ground - by, rz)
            await self.tell(cid, "Off to the blast range, far from your builds: the TNT goes off there!")
            await self.e._arrive(who, view, look, await self.e._fly(who, view, look))
            x, y, z = bx + rx, ground, bz + rz
            cid = who
        await self.e._lay_by_hand(cid, f"setblock {{x{x - bx}}} {{y{y - by}}} {{z{z - bz}}} minecraft:tnt")
        await self.rcon(f"setblock {x} {y} {z} minecraft:air")
        await self.rcon(f"summon minecraft:tnt {x + 0.5} {y} {z + 0.5} {{fuse:60s}}")
        self.remember(f"{self.e.names[cid]} lit TNT")
        CLIPS.mark("tnt", f"{self.e.names[cid]} lit TNT", 2.5)
        await self.tell(cid, "The TNT is lit and blows up in 3 seconds! React out loud.")

    async def e_ground(self, x: int, z: int, near: int) -> int:
        from .minecraft_mode import ground_height

        await self.e._hold_site("tnt", x - 8, z - 8, x + 8, z + 8)
        return await ground_height(x, z, near)

    # ------------------------------------------------------------ the race
    async def build_course(self) -> bool:
        if self.course_built:
            return True
        if not await self.e.projects.ensure_loaded():
            return False
        for x, block in HOOPS:
            for command in hoop(x, block):
                await self.e.projects.place_one(command)
        self.course_built = True
        return True

    async def race(self, starter: str) -> None:
        here = [c for c in self.e.cast if time.time() - self.e._seen_at.get(c, 0) < 15]
        if self.racing or len(here) < 2 or not self.e.projects.state.get("base"):
            if starter in self.e.names and len(here) < 2:
                await self.tell(starter, "Your friend is not in the world right now, so no race yet.")
            return
        self.racing = True
        hold = time.time() + 60  # the builder leaves them alone during the race
        for c in here:
            self.e._chose_at[c] = hold
        try:
            await self._race(here)
        except Exception as exc:  # pragma: no cover - live game dependent
            logger.warning(f"Minecraft: the race failed: {exc}")
        finally:
            for c in here:
                self.e._chose_at[c] = time.time() - 60  # free again
            self.racing = False

    async def _race(self, racers: list[str]) -> None:
        bx, by, bz = self.e.projects.state["base"]
        if not await self.build_course():
            return
        start = (COURSE_X[0] - 6, COURSE_Y - 0.5, COURSE_Z)
        finish = (COURSE_X[1] + 6, COURSE_Y - 0.5, COURSE_Z)
        look = (COURSE_X[1], COURSE_Y, COURSE_Z)
        # to the start line, side by side (each in her own lane, see _own_spot)
        flights = [self.e._fly(c, start, look) for c in racers]
        times = await asyncio.gather(*flights)
        await asyncio.gather(*(self.e._arrive(c, start, look, t) for c, t in zip(racers, times)))
        lanes = {c: self.e._target.get(c, (bx + start[0], by + start[1], bz + start[2])) for c in racers}
        # Speed for each racer: a potion she drank counts; the others get a
        # random boost, never the same as hers, so there is always a winner.
        levels: dict[str, int] = {}
        for c in racers:
            mine = self.levels.get(c)
            if mine is None:
                taken = set(levels.values()) | {v for k, v in self.levels.items() if k in racers}
                mine = random.choice([lv for lv in (0, 1, 2) if lv not in taken] or [0])
            levels[c] = mine
            if mine:
                await self.rcon(f"effect give {self.e.names[c]} minecraft:speed 20 {mine - 1}")
        await self.rcon("title @a times 0 25 5")
        names = " vs ".join(self.e.names[c] for c in racers)
        await self.rcon(f'title @a subtitle {{"text":"{names}","color":"white"}}')
        for n, color in (("3", "red"), ("2", "gold"), ("1", "yellow")):
            await self.rcon(f'title @a title {{"text":"{n}","color":"{color}","bold":true}}')
            await self.rcon("execute as @a at @s run playsound minecraft:block.note_block.pling master @s ~ ~ ~ 1 1")
            await asyncio.sleep(1.0)
        await self.rcon('title @a title {"text":"GO!","color":"green","bold":true}')
        await self.rcon("execute as @a at @s run playsound minecraft:block.note_block.pling master @s ~ ~ ~ 1 2")
        distance = finish[0] - start[0]
        for c in racers:
            x, y, z = lanes[c]
            fx = bx + finish[0]
            self.e._target[c] = (fx, y, z)
            await self.e._command(c, f"!flyTo({fx:.1f}, {y:.1f}, {z:.1f}, {fx + 10:.1f}, {y + 1.6:.1f}, {z:.1f}, -60)")
        winner = max(racers, key=lambda c: levels[c])
        loser = next(c for c in racers if c != winner)
        await asyncio.sleep(finish_seconds(levels[winner], distance) + 0.3)
        w, lost = self.e.names[winner], self.e.names[loser]
        await self.rcon(f'title @a title {{"text":"{w} wins!","color":"gold","bold":true}}')
        await self.rcon(f'title @a subtitle {{"text":"{lost} was {finish_seconds(levels[loser], distance) - finish_seconds(levels[winner], distance):.1f} s behind","color":"white"}}')
        fx, fy, fz = bx + finish[0], by + finish[1] + 3, bz + finish[2]
        await self.rcon(
            f"summon minecraft:firework_rocket {fx} {fy} {fz} "
            '{LifeTime:15,FireworksItem:{id:"minecraft:firework_rocket",count:1,components:{"minecraft:fireworks":'
            '{flight_duration:1,explosions:[{shape:"large_ball",colors:[I;16766720,16711680],has_twinkle:1b}]}}}}'
        )
        self.remember(f"{w} won the sky race against {lost}")
        self.e._count(winner=winner)
        CLIPS.mark("race", f"{w} won the sky race against {lost}", 3.0)
        self.e._exclaim(winner, "won")
        logger.info(f"Minecraft: {w} won the race ({levels[winner]} vs {levels[loser]})")
        await self.tell(winner, f"You WON the sky race against {lost}! Brag about it out loud.")
        await self.tell(loser, f"You LOST the sky race to {w}. React out loud: demand a rematch or blame something.")


RACE_ASK = re.compile(r"\b(race|racing)\b", re.I)
TNT_ASK = re.compile(r"\b(tnt|blow (?:it |something |stuff )?up|explode|explosion|kaboom)\b", re.I)


def wants_tnt(text: str) -> bool:
    from .minecraft_asks import wants_tnt as asked  # a request, not "this channel will blow up"

    return asked(text)


def wants_race(text: str) -> bool:
    from .minecraft_asks import wants_race as asked

    return asked(text)


def potion_ask(text: str) -> Optional[str]:
    """'Mika drink a potion of invisibility', 'drink invisible potion mika',
    'splash luna with glowing' -> the effect (or None)."""
    low = (text or "").lower()
    if not re.search(r"\b(potion|potions|drink|splash|brew)\b", low):
        return None
    words = re.findall(r"[a-z_]+", low)
    for a, b in zip(words, words[1:] + [""]):
        for name in (effect_name(f"{a}_{b}"), effect_name(a)):
            if name in EFFECTS:
                return name
    if re.search(r"\b(?:something|anything|random|mystery|surprise|any|whatever)\b", low) or re.search(
            r"\bsplash\b", low):
        return random.choice(MYSTERY)  # "splash something on that chicken": a surprise
    return None
