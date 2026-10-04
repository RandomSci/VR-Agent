"""Eyes for Mika and Luna, from the game itself (free: no vision model).

Every EYES_SECONDS the engine asks each girl's bot what is around her
(!senses, sent from DIRECTOR, no AI call): the block she is looking at,
every creature and player within 32 blocks with how far and where (in front,
to the left, behind), what she stands on, how high she is, day or night. It
comes back as '[VR] sees {...}' and is turned into a sentence that goes into
everything she says. When something new shows up (a spider, a player close
in front) she is told and reacts out loud. "What do you see?" from chat makes
her look right away. VR_MINECRAFT_EYES=0 turns it off.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:  # pragma: no cover
    from .minecraft_mode import MinecraftEngine

EYES_SECONDS = 15.0  # each girl looks this often (free: no AI call)
SIGHT_FRESH = 60.0  # a sight older than this is not used any more
NOTABLE_GAP = 90.0  # at most one "react to what you see" per girl this often
REACTIONS_PER_HOUR = 8  # both girls together (each reaction is an AI call and a spoken line)
LOOK_WAIT = 3.0  # "what do you see?" waits this long for her answer from the game
HOSTILE = {"spider", "cave spider", "zombie", "skeleton", "creeper", "enderman", "witch", "slime", "phantom",
           "drowned", "husk", "stray", "pillager", "zombie villager", "silverfish", "blaze", "ghast"}
LOOK_ASK = re.compile(
    r"\b(what (?:do|can) you see|do you see|can you see|look (?:at|around|behind|left|right|up|down)|"
    r"what(?:'s| is) (?:that|there|in front|behind|around)|turn around|behind you|watch out)\b",
    re.I,
)


def wants_look(text: str) -> bool:
    return bool(LOOK_ASK.search(text or ""))


def sentence(seen: dict[str, Any]) -> str:
    """The game's answer as one plain sentence."""
    parts = []
    if seen.get("looking"):
        parts.append(f"Looking at {str(seen['looking']).replace('_', ' ')}.")
    near = [f"{str(n.get('name', '?')).replace('_', ' ')} {n.get('d', '?')} blocks {n.get('where', '')}".strip()
            for n in (seen.get("near") or [])[:6]]
    players = [f"{p.get('name', '?')} {p.get('d', '?')} blocks {p.get('where', '')}".strip()
               for p in (seen.get("players") or [])[:4]]
    if near:
        parts.append("Creatures: " + ", ".join(near) + ".")
    if players:
        parts.append("Players: " + ", ".join(players) + ".")
    where = []
    if seen.get("on"):
        where.append(f"standing on {str(seen['on']).replace('_', ' ')}" if seen["on"] != "air" else "flying")
    if seen.get("y") is not None:
        where.append(f"height {seen['y']}")
    if isinstance(seen.get("time"), (int, float)):
        where.append("night" if 13000 <= int(seen["time"]) % 24000 <= 23000 else "day")
    if where:
        parts.append(", ".join(where).capitalize() + ".")
    return " ".join(parts) or "Nothing special around."


class Eyes:
    def __init__(self, engine: "MinecraftEngine") -> None:
        self.e = engine
        self.sight: dict[str, tuple[float, str]] = {}
        self.things: dict[str, set[str]] = {}
        self.nearby: dict[str, list[str]] = {}  # creature types from the bots' own state
        self._notable_at: dict[str, float] = {}
        self._waiting: dict[str, asyncio.Event] = {}
        self._known: set[str] = set()
        self.nearest: dict[str, Any] = {}  # (name, is a player) of the closest thing she saw
        self.looking_at: dict[str, tuple] = {}  # (x, y, z, block, when) of the block in the middle of her view  # creature kinds already reacted to this stream
        self._reactions: list[float] = []

    @staticmethod
    def enabled() -> bool:
        return os.environ.get("VR_MINECRAFT_EYES", "1").strip().lower() not in ("0", "false", "off", "no")

    @staticmethod
    def every() -> float:
        try:
            return max(5.0, float(os.environ.get("VR_MINECRAFT_EYES_SECONDS", "") or EYES_SECONDS))
        except ValueError:
            return EYES_SECONDS

    def describe_for(self, cid: str) -> str:
        """Her sight for prompts ('' when there is nothing fresh)."""
        at, text = self.sight.get(cid, (0.0, ""))
        if text and time.time() - at < SIGHT_FRESH:
            return text
        near = self.nearby.get(cid) or []
        return ("Creatures close to her: " + ", ".join(near[:6]) + ".") if near else ""

    def note_state(self, cid: str, state: dict[str, Any]) -> None:
        nearby = state.get("nearby") or {}
        kinds = nearby.get("entityTypes") or nearby.get("entities") or []
        if isinstance(kinds, list):
            self.nearby[cid] = [str(k).replace("_", " ") for k in kinds if str(k) not in ("item", "experience_orb")][:8]

    async def run(self) -> None:
        if not self.enabled():
            return
        await asyncio.sleep(20)
        while True:
            for cid in list(self.e.cast):
                if time.time() - self.e._seen_at.get(cid, 0) < 15:
                    await self.e._command(cid, "!senses")
                await asyncio.sleep(self.every() / max(1, len(self.e.cast)))

    async def look(self, cid: str, reason: str = "") -> str:
        """Ask her bot right now and wait a moment for the answer."""
        event = self._waiting.setdefault(cid, asyncio.Event())
        event.clear()
        await self.e._command(cid, "!senses")
        try:
            await asyncio.wait_for(event.wait(), timeout=LOOK_WAIT)
        except asyncio.TimeoutError:
            pass
        if self.nearest.get(cid):  # "what do you see?": she looks at it while she answers
            what, player = self.nearest[cid]
            await self.e.look_toward(cid, what, player=player)
        return self.describe_for(cid)

    async def saw(self, cid: str, raw: str) -> None:
        """'[VR] sees {...}' from her bot."""
        try:
            seen = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(seen, dict):
            return
        # Not news: her friend (always about 5 blocks away, she was "new"
        # every time she came closer) and your camera (a spectator player
        # the girls would call a stranger).
        camera = str(getattr(self.e, "camera_player", "")).lower()
        seen["players"] = [q for q in seen.get("players") or [] if str(q.get("name", "")).lower() != camera]
        text = sentence(seen)
        friend = str(self.e.names.get(self.e._friend(cid), "")).lower()
        names = {str(n.get("name", "")).replace("_", " ") for n in seen.get("near") or []}
        players = {str(q.get("name", "")) for q in seen.get("players") or []
                   if (q.get("d") if q.get("d") is not None else 99) <= 6 and str(q.get("name", "")).lower() != friend}
        old = self.things.get(cid, set())
        self.sight[cid] = (time.time(), text)
        self.things[cid] = names | players
        spot = seen.get("at")
        if isinstance(spot, list) and len(spot) == 3 and all(isinstance(v, (int, float)) for v in spot):
            block = str(seen.get("looking", "")).split(" ")[0]
            self.looking_at[cid] = (int(spot[0]), int(spot[1]), int(spot[2]), block, time.time())
        nearest = (seen.get("near") or [None])[0] or next(
            (q for q in seen.get("players") or [] if str(q.get("name", "")).lower() != friend), None)
        self.nearest[cid] = (str(nearest.get("name", "")), nearest in (seen.get("players") or [])) if nearest else None
        if cid in self._waiting:
            self._waiting[cid].set()
        new = (names | players) - old
        # worth a reaction: a monster, a creature kind not seen yet this stream, a real player close by
        worth = (new & HOSTILE) or ((new & names) - self._known) or (new & players)
        now = time.time()
        self._reactions = [t for t in self._reactions if now - t < 3600]
        if (worth and now - self._notable_at.get(cid, 0) > NOTABLE_GAP
                and len(self._reactions) < REACTIONS_PER_HOUR):
            self._known |= names
            self._reactions.append(now)
            self._notable_at[cid] = time.time()
            name = self.e.names[cid]
            self.e._remember(f"{name} sees: {text[:120]}")
            logger.info(f"Minecraft eyes: {name} sees {', '.join(sorted(worth))}")
            # she turns to it first: what she talks about is what the stream shows
            target = sorted(worth & HOSTILE) or sorted(worth & names) or sorted(worth)
            await self.e.look_toward(cid, target[0], player=target[0] in players)
            await self.e.link.emit("send-message", name, {"from": "system", "message": (
                f"You see right now: {text} React to what is new out loud in one short line.")})

    async def close(self) -> None:
        return None
