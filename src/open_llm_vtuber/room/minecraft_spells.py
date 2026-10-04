"""Spells: real magic in the world, cast with a wave of her hand.

Chat types "Mika cast lightning on Luna", "make Luna giant", "summon a
parrot", "mix levitation and fireworks"; the girls can cast them too
(!castSpell). Every spell is real game commands (no AI call): she turns to
the target and waves, the spell sound plays, particles fly, and it happens.
Nothing here can hurt a build: lightning never starts fires (fire is off),
the freeze is particles and slowness, trees only grow on free ground, and
summoned creatures are friendly and kept to a small crowd.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

from ..live.clip_marks import CLIPS

if TYPE_CHECKING:  # pragma: no cover
    from .minecraft_mode import MinecraftEngine

# the spell, what it does (the girls read this list), what words call it
SPELLS: dict[str, tuple[str, tuple[str, ...]]] = {
    "lightning": ("a lightning bolt strikes next to the target", ("lightning", "thunder bolt", "thunderbolt", "zap",
                                                                    "smite", "strike")),
    "levitate": ("the target floats up into the air", ("levitate", "levitation", "float", "fly up", "lift")),
    "freeze": ("ice and snowflakes burst around the target, it slows down", ("freeze", "frost", "ice", "frozen",
                                                                            "blizzard")),
    "summon": ("a friendly creature appears (parrot, cat, wolf, fox, axolotl, allay, bee, rabbit, frog, panda, "
               "snow golem, iron golem...)", ("summon", "spawn", "conjure", "call forth")),
    "storm": ("a thunderstorm for one minute", ("storm", "thunderstorm")),
    "rain": ("rain for one minute", ("rain", "make it rain")),
    "night": ("night falls for one minute", ("night", "darkness", "dark")),
    "fireworks": ("a fireworks show over the target", ("fireworks", "firework", "celebrate")),
    "grow": ("a tree and flowers grow on free ground in front of her", ("grow", "tree", "flowers", "bloom",
                                                                        "nature")),
    "giant": ("the target grows huge for 20 seconds", ("giant", "huge", "big", "bigger", "enlarge", "grow huge")),
    "tiny": ("the target shrinks tiny for 20 seconds", ("tiny", "small", "shrink", "smaller", "mini")),
    "heal": ("hearts and healing for the target", ("heal", "healing", "hearts", "love")),
    "sparkle": ("sparkles and stars around the target", ("sparkle", "sparkles", "glitter", "magic", "shine")),
}
CREATURES = ("parrot", "cat", "wolf", "fox", "axolotl", "allay", "bee", "rabbit", "frog", "panda", "sheep", "pig",
             "chicken", "cow", "horse", "camel", "turtle", "snow_golem", "iron_golem", "llama", "goat", "ocelot",
             "armadillo", "sniffer", "dolphin", "mooshroom", "polar_bear", "villager")
CREATURE_WORDS = {"golem": "iron_golem", "snowman": "snow_golem", "snow golem": "snow_golem", "iron golem": "iron_golem",
                  "kitty": "cat", "kitten": "cat", "dog": "wolf", "puppy": "wolf", "bunny": "rabbit", "birb": "parrot",
                  "bird": "parrot", "fairy": "allay", "bear": "polar_bear"}
SPELL_ASK = re.compile(r"\b(?:cast|spell|spells|magic|enchant|alchemy|abracadabra|wizard|witch)\b", re.I)
MIX_ASK = re.compile(r"\b(?:mix|combine|fuse|together with|plus|\+)\b", re.I)
MAX_SUMMONED = 14  # summoned creatures at once (the oldest go)
SPELL_GAP = 4.0  # one spell at a time, at most this often
GIRL_SPELL_GAP = 300.0  # a girl's own spell idea at most this often (chat any time)
SIZE_SECONDS = 20.0
WEATHER_SECONDS = 60.0
TAG = "vr_spell"


def spells_asked(text: str) -> list[str]:
    """'Mika cast lightning on Luna' -> ['lightning']; 'mix levitation and
    fireworks' -> ['levitate', 'fireworks']. Only when it is about magic (a
    spell word, "summon", "make X giant"...), not 'I love rain'."""
    low = (text or "").lower()
    found: list[tuple[int, str]] = []
    for name, (_what, words) in SPELLS.items():
        for word in words:
            m = re.search(rf"\b{re.escape(word)}\b", low)
            if m:
                found.append((m.start(), name))
                break
    from .minecraft_fun import EFFECTS, effect_name

    for m in re.finditer(r"[a-z_]+", low):  # "an invisible spell", "a speed spell", "glow spell"
        name = effect_name(m.group(0))
        covered = {"levitation": "levitate", "regeneration": "heal", "slowness": "freeze"}  # a real spell does it
        if name in EFFECTS and not any(n in (f"effect:{name}", covered.get(name)) for _i, n in found):
            found.append((m.start(), f"effect:{name}"))
    if not found:
        return []
    names = [n for _i, n in sorted(found)]
    magic = bool(SPELL_ASK.search(low)) or bool(re.search(r"\b(?:summon|conjure|spawn)\b", low)) or bool(
        re.search(r"\bmake (?:her|him|luna|mika|them|me|yourself|the (?:cow|pig|sheep|chicken|golem|horse|camel|rabbit|"
                  r"fox|wolf|cat|parrot|llama|goat|villager|mob|animal)) (?:giant|huge|tiny|small|big|bigger|smaller|"
                  r"float|levitate|invisible|glow|glowing|fast|faster|jump|slow|dizzy|strong)\b", low))
    if not magic:
        return []
    if not MIX_ASK.search(low):
        names = names[:1]
    return list(dict.fromkeys(names))[:3]


def creature_asked(text: str) -> str:
    low = (text or "").lower()
    for word, kind in CREATURE_WORDS.items():
        if re.search(rf"\b{word}s?\b", low):
            return kind
    for kind in CREATURES:
        if re.search(rf"\b{kind.replace('_', ' ')}s?\b", low):
            return kind
    return random.choice(("parrot", "allay", "axolotl", "cat", "fox"))


def target_asked(text: str, names: dict[str, str], caster: str) -> str:
    """'on Luna' -> 'luna'; 'on me/yourself' -> the caster; 'on the cow/mob' -> 'mob'; else 'friend'."""
    low = (text or "").lower()
    for cid, name in names.items():
        if re.search(rf"\b(?:on|at|to|make) {re.escape(name.lower())}\b", low):
            return cid
    if re.search(r"\b(?:on|at) (?:yourself|you|herself)\b", low):
        return caster
    animal = (r"mob|animal|creature|cow|pig|sheep|chicken|golem|horse|camel|rabbit|spider|zombie|skeleton|creeper|"
              r"llama|goat|fox|wolf|cat|parrot|villager|squid|salmon|cod|bat|bee")
    if re.search(rf"\b(?:on|at|make) (?:the |a |that )?(?:{animal}|it)\b", low) or (
            re.search(rf"\b(?:the|that|this) (?:{animal})s?\b", low)
            and not re.search(r"\b(?:summon|spawn|conjure)\b", low)):
        return "mob"
    if re.search(r"\b(?:on|at|over) (?:the |our |your )?(?:build|tower|castle|house|it)\b", low):
        return "build"
    return "friend"


class Spells:
    def __init__(self, engine: "MinecraftEngine") -> None:
        self.e = engine
        self._cast_at = 0.0
        self._girl_at = 0.0
        self._lock = asyncio.Lock()
        self._fire_off = False

    async def rcon(self, command: str) -> Any:
        from . import minecraft_mode as mm

        return await mm.rcon_command(command, reply=True)

    def list_for_girls(self) -> str:
        return "; ".join(f"{name}: {what}" for name, (what, _w) in SPELLS.items())

    async def _where(self, caster: str, target: str) -> tuple[Optional[str], Optional[tuple[float, float, float]]]:
        """The target's selector (for effects) and position."""
        e = self.e
        if target in e.names and target in e._pos:
            return e.names[target], e._pos[target]
        if target == "mob" and caster in e._pos:
            from .minecraft_mode import _numbers

            sel = "@e[type=!player,type=!item,type=!armor_stand,type=!marker,sort=nearest,limit=1,distance=..24]"
            pos = _numbers(await self.rcon(f"execute at {e.names[caster]} run data get entity {sel} Pos"))
            if len(pos) >= 3:
                return f"@e[type=!player,type=!item,type=!armor_stand,type=!marker,sort=nearest,limit=1,x={pos[0]:.1f},y={pos[1]:.1f},z={pos[2]:.1f},distance=..2]", (pos[0], pos[1], pos[2])
        if target == "build" and e.build_focus:
            return None, tuple(e.build_focus)
        friend = e._friend(caster)
        if friend != caster and friend in e._pos:
            return e.names[friend], e._pos[friend]
        return None, e._pos.get(caster)

    async def cast(self, caster: str, spells: list[str], target: str = "friend", who: str = "",
                   creature: str = "", girl: bool = False) -> str:
        """Cast one spell (or mix up to three). Returns what happened ('' when it could not)."""
        e = self.e
        now = time.time()
        if girl and now - self._girl_at < GIRL_SPELL_GAP:
            await e.link.emit("send-message", e.names[caster], {"from": "system", "message": (
                "You just cast a spell. Build first; chat can ask for the next one any time.")})
            return ""
        if caster not in e._pos or not spells:
            return ""
        async with self._lock:
            wait = SPELL_GAP - (time.time() - self._cast_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._cast_at = time.time()
            if girl:
                self._girl_at = time.time()
            if not self._fire_off:  # lightning never sets anything on fire
                await self.rcon("gamerule doFireTick false")
                self._fire_off = True
            sel, pos = await self._where(caster, target)
            if pos is None:
                return ""
            name = e.names[caster]
            x, y, z = pos
            # she looks at it and waves (her eyes are the stream: it is on screen)
            from .minecraft_mode import CHOICE_HOLD

            e._chose_at[caster] = time.time() + 8 - CHOICE_HOLD  # the build waits 8 s while she casts
            await e._command(caster, f"!gesture({x:.1f}, {y + 1:.1f}, {z:.1f}, 0)")
            await self.rcon(f"execute at {name} run playsound minecraft:entity.evoker.cast_spell player @a ~ ~ ~ 1 1")
            await self._beam(caster, (x, y + 1.0, z))
            await asyncio.sleep(0.4)
            done = []
            for spell in spells[:3]:
                try:
                    text = await self._one(spell, caster, sel, (x, y, z), creature)
                except Exception as exc:  # pragma: no cover - live game dependent
                    logger.warning(f"Minecraft: spell {spell} failed: {exc}")
                    text = ""
                if text:
                    done.append(text)
            if len(spells) > 1 and done:
                await self.rcon(f"particle minecraft:witch {x:.1f} {y + 1:.1f} {z:.1f} 1 1 1 0.1 120 force")
                await self.rcon(f"playsound minecraft:block.brewing_stand.brew player @a {x:.1f} {y:.1f} {z:.1f} 1 1")
            if not done:
                return ""
            what = " and ".join(done)
            mixed = " (mixed together)" if len(done) > 1 else ""
            e._remember(f"{name} cast {'+'.join(spells)}{mixed}" + (f" for {who}" if who else "") + f": {what}")
            CLIPS.mark("spell", f"{name} cast {'+'.join(spells)}", 2.0)
            logger.info(f"Minecraft: {name} cast {'+'.join(spells)} ({what})")
            return f"{what}{mixed}"

    async def _beam(self, caster: str, to: tuple[float, float, float]) -> None:
        """Sparkles from where her hand is (low right of her view) to the
        target: in first person nobody sees her arm, so the magic itself
        has to come out of the bottom right of the screen."""
        import math

        here = self.e._pos.get(caster)
        if not here:
            return
        dx, dz = to[0] - here[0], to[2] - here[2]
        length = math.hypot(dx, dz) or 1.0
        fx, fz = dx / length, dz / length
        rx, rz = -fz, fx  # her right hand
        hand = (here[0] + fx * 0.9 + rx * 0.45, here[1] + 1.25, here[2] + fz * 0.9 + rz * 0.45)
        steps = max(4, min(24, int(math.dist(hand, to) * 1.5)))
        for i in range(steps + 1):
            t = i / steps
            px, py, pz = (hand[k] + (to[k] - hand[k]) * t for k in range(3))
            await self.rcon(f"particle minecraft:end_rod {px:.2f} {py:.2f} {pz:.2f} 0.03 0.03 0.03 0.01 2 force")
            if i % 3 == 0:
                await self.rcon(f"particle minecraft:enchanted_hit {px:.2f} {py:.2f} {pz:.2f} 0.1 0.1 0.1 0.1 3 force")

    async def _one(self, spell: str, caster: str, sel: Optional[str], pos: tuple[float, float, float],
                   creature: str) -> str:
        e = self.e
        x, y, z = pos
        target_name = sel if sel and not sel.startswith("@") else ("the creature" if sel else "there")
        if spell.startswith("effect:") and sel:
            from .minecraft_fun import EFFECTS

            name = spell.split(":", 1)[1]
            seconds, level = EFFECTS.get(name, (15, 0))
            girl = next((c for c, n in e.names.items() if n == sel), None)
            if girl:  # a girl: like a potion (the effect timer shows on screen)
                await e.fun.apply(girl, name)
            else:
                await self.rcon(f"effect give {sel} minecraft:{name} {seconds} {level}")
            await self.rcon(f"particle minecraft:effect {x:.1f} {y + 1:.1f} {z:.1f} 0.5 1 0.5 0.1 80 force")
            return f"{target_name} got {name.replace('_', ' ')} for {seconds} seconds"
        if spell == "lightning":
            await self.rcon(f"summon minecraft:lightning_bolt {x + 2:.1f} {y:.1f} {z + 1:.1f}")
            return f"lightning struck right next to {target_name}"
        if spell == "levitate" and sel:
            await self.rcon(f"effect give {sel} minecraft:levitation 4 1")
            await self.rcon(f"particle minecraft:end_rod {x:.1f} {y + 1:.1f} {z:.1f} 0.5 1 0.5 0.05 60 force")
            return f"{target_name} floated up into the air"
        if spell == "freeze":
            if sel:
                await self.rcon(f"effect give {sel} minecraft:slowness 6 3")
            await self.rcon(f"particle minecraft:snowflake {x:.1f} {y + 1:.1f} {z:.1f} 1.5 1.5 1.5 0.05 300 force")
            await self.rcon(f"playsound minecraft:block.glass.break player @a {x:.1f} {y:.1f} {z:.1f} 1 0.7")
            return f"{target_name} got frozen in a burst of ice"
        if spell == "summon":
            kind = creature or "parrot"
            count = await self.rcon(f"execute if entity @e[tag={TAG}]")
            many = re.search(r"count:?\s*(\d+)", str(count or ""))
            if many and int(many.group(1)) >= MAX_SUMMONED:
                await self.rcon(f"kill @e[tag={TAG},sort=furthest,limit=4]")  # the oldest crowd goes
            cx, cy, cz = e._pos[caster]
            fx, fz = e._facing(caster)
            sx, sz = cx + fx * 3, cz + fz * 3
            await self.rcon(f"summon minecraft:{kind} {sx:.1f} {cy:.1f} {sz:.1f} {{Tags:[\"{TAG}\"],PersistenceRequired:1b}}")
            await self.rcon(f"particle minecraft:poof {sx:.1f} {cy + 0.5:.1f} {sz:.1f} 0.4 0.4 0.4 0.02 30 force")
            return f"a {kind.replace('_', ' ')} appeared"
        if spell in ("storm", "rain"):
            await self.rcon(f"weather {'thunder' if spell == 'storm' else 'rain'} {int(WEATHER_SECONDS)}")

            async def clear() -> None:  # the weather cycle is off: it would stay like that
                await asyncio.sleep(WEATHER_SECONDS)
                await self.rcon("weather clear")

            from .minecraft_mode import _soon

            _soon(clear())
            return "a thunderstorm rolled in" if spell == "storm" else "it started to rain"
        if spell == "night":
            await self.rcon("time set night")

            async def dawn() -> None:
                await asyncio.sleep(WEATHER_SECONDS)
                await self.rcon("time set day")

            from .minecraft_mode import _soon

            _soon(dawn())
            return "night fell for a minute"
        if spell == "fireworks":
            for i in range(5):
                colors = random.sample([16711680, 16753920, 16776960, 65280, 3368703, 10040319, 16738740], 2)
                await self.rcon(
                    f"summon minecraft:firework_rocket {x + random.uniform(-3, 3):.1f} {y + 2:.1f} "
                    f"{z + random.uniform(-3, 3):.1f} "
                    '{LifeTime:' + str(14 + i * 3) + ',FireworksItem:{id:"minecraft:firework_rocket",count:1,'
                    'components:{"minecraft:fireworks":{flight_duration:1,explosions:[{shape:"'
                    + random.choice(["large_ball", "star", "burst", "small_ball"]) + '",colors:[I;'
                    + ",".join(map(str, colors)) + '],has_twinkle:1b}]}}}}')
            return f"a fireworks show burst over {target_name}"
        if spell == "grow":
            from .minecraft_mode import ground_height

            cx, cy, cz = e._pos[caster]
            fx, fz = e._facing(caster)
            gx, gz = int(cx + fx * 7), int(cz + fz * 7)
            if e.near_builds(gx, gz, 4):
                await self.rcon(f"particle minecraft:happy_villager {gx} {cy:.1f} {gz} 2 1 2 0.1 80 force")
                return "green sparkles flew (no free ground for a tree here, too close to the builds)"
            gy = await ground_height(gx, gz, int(cy))
            await self.rcon(f"place feature minecraft:cherry {gx} {gy} {gz}")
            await self.rcon(f"place feature minecraft:flower_meadow {gx + 3} {gy} {gz + 2}")
            await self.rcon(f"particle minecraft:happy_villager {gx} {gy + 2} {gz} 2 2 2 0.1 120 force")
            return "a cherry tree and flowers grew out of the ground"
        if spell in ("giant", "tiny") and sel:
            if sel == e.names.get(e._camera_girl()):
                return ""  # her eyes are the stream: not on the camera
            size = 3.0 if spell == "giant" else 0.35
            await self.rcon(f"attribute {sel} minecraft:scale base set {size}")
            await self.rcon(f"particle minecraft:cloud {x:.1f} {y + 1:.1f} {z:.1f} 0.6 1 0.6 0.05 60 force")

            async def back() -> None:
                await asyncio.sleep(SIZE_SECONDS)
                await self.rcon(f"attribute {sel} minecraft:scale base set 1")

            from .minecraft_mode import _soon

            _soon(back())
            return f"{target_name} turned {'GIANT' if spell == 'giant' else 'tiny'} for 20 seconds"
        if spell == "heal":
            if sel:
                await self.rcon(f"effect give {sel} minecraft:regeneration 8 1")
            await self.rcon(f"particle minecraft:heart {x:.1f} {y + 1.5:.1f} {z:.1f} 0.6 0.6 0.6 0.1 25 force")
            return f"hearts floated around {target_name}"
        if spell == "sparkle":
            await self.rcon(f"particle minecraft:totem_of_undying {x:.1f} {y + 1:.1f} {z:.1f} 0.6 1 0.6 0.4 150 force")
            await self.rcon(f"playsound minecraft:block.amethyst_block.chime player @a {x:.1f} {y:.1f} {z:.1f} 1 1")
            return f"sparkles burst around {target_name}"
        return ""
