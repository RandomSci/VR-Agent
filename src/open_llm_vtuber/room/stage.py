"""Stage Director: where each character stands, and short repositioning.

Positions are authoritative state (``CharacterState.x`` and ``.zone``). The
renderer only animates to the positions it is sent, so what is on screen,
what the state says and what the characters are told always match.

Movement is deliberately modest. The Live2D models have no walk cycle, so
repositioning is a short, eased glide with a gentle bob (``walk``), and for
longer distances Mika can use a small magic blink instead (``magic``).
Long journeys are never shown as walking: the Adventure Director changes the
world around the characters instead.

Moves are refused while a game is on the board (everyone stays at their game
spot), and characters never end up closer than MIN_CHARACTER_GAP.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Optional

from . import events as ev
from .world_catalog import MIN_CHARACTER_GAP, ZONE_ORDER, nearest_zone

if TYPE_CHECKING:  # pragma: no cover
    from .session import RoomSession

GLIDE_SPEED = 0.07  # stage widths per second: slow and calm (about 135 px/s)
MIN_MOVE_SECONDS = 1.6
MAX_MOVE_SECONDS = 6.0
MAGIC_BLINK_SECONDS = 1.8
MAGIC_BLINK_DISTANCE = 0.32  # longer moves than this use Mika's blink
GESTURE_SECONDS = {"hop": 0.8, "jump": 1.4, "dance": 4.5}
EDGE = 0.1


class StageDirector:
    def __init__(self, session: "RoomSession", clock=time.time):
        self.session = session
        self.clock = clock
        self.moves = 0
        self.refused = 0
        for profile in session.room.characters:
            state = session.state.characters.get(profile.id)
            if state:
                x = max(EDGE, min(1 - EDGE, float(profile.layout.x)))
                state.x = state.home_x = x
                state.zone = nearest_zone(x)
        session.bus.subscribe(ev.GAME_STARTED, self.on_game_started)

    # ------------------------------------------------------------------
    def locked_reason(self, character_id: str) -> str:
        """Why a character may not reposition right now ('' when she may)."""
        if self.session.show.engine.playing:
            return "a game is on the board, so everyone stays at their game spot until it ends"
        state = self.session.state.characters.get(character_id)
        if not state or not state.is_available():
            return "she is not on stage right now"
        return ""

    def _name(self, character_id: str) -> str:
        profile = self.session.room.get(character_id)
        return profile.name if profile else character_id

    def _others(self, character_id: str):
        return [
            c for cid, c in self.session.state.characters.items() if cid != character_id
        ]

    def _free_x(self, character_id: str, target: float) -> float:
        """Nudge the target so she keeps the minimum gap to everyone else."""
        target = max(EDGE, min(1 - EDGE, target))
        for other in self._others(character_id):
            if abs(other.x - target) < MIN_CHARACTER_GAP:
                side = -1 if target <= other.x else 1
                target = other.x + side * MIN_CHARACTER_GAP
        return max(EDGE, min(1 - EDGE, target))

    def _clear_path_for(
        self, character_id: str, target: float, now: float
    ) -> list[dict[str, Any]]:
        """If someone stands where she is going, that someone steps aside."""
        ops: list[dict[str, Any]] = []
        for other in self._others(character_id):
            if abs(other.x - target) >= MIN_CHARACTER_GAP:
                continue
            side = 1 if other.x >= target else -1
            spot = target + side * MIN_CHARACTER_GAP
            if not EDGE <= spot <= 1 - EDGE:
                spot = target - side * MIN_CHARACTER_GAP
            ops += self._glide(
                other.id, max(EDGE, min(1 - EDGE, spot)), now, style="walk"
            )
        return ops

    def _glide(
        self, character_id: str, x: float, now: float, style: str = ""
    ) -> list[dict[str, Any]]:
        state = self.session.state.characters[character_id]
        distance = abs(x - state.x)
        if distance < 0.01:
            return []
        profile = self.session.room.get(character_id)
        magic = "magic" in (profile.abilities if profile else ())
        style = style or (
            "magic" if magic and distance > MAGIC_BLINK_DISTANCE else "walk"
        )
        seconds = (
            MAGIC_BLINK_SECONDS
            if style == "magic"
            else max(MIN_MOVE_SECONDS, min(MAX_MOVE_SECONDS, distance / GLIDE_SPEED))
        )
        state.x = x
        state.zone = nearest_zone(x, self.session.world.zones())
        state.moving_until = now + seconds
        self.moves += 1
        return [
            {
                "op": "stage",
                "character": character_id,
                "move": "walk",
                "x": round(x, 4),
                "ms": int(seconds * 1000),
                "style": style,
            }
        ]

    # ------------------------------------------------------------------
    def move_to(
        self,
        character_id: str,
        zone: Optional[str] = None,
        x: Optional[float] = None,
        why: str = "",
        style: str = "",
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        """Reposition to a zone (or x). Returns (performed, reason, ops)."""
        reason = self.locked_reason(character_id)
        if reason:
            self.refused += 1
            return False, reason, []
        if x is None:
            x = self.session.world.zone_x(zone) if zone else None
        if x is None:
            self.refused += 1
            return False, f"there is no spot called {zone} here", []
        now = self.clock()
        state = self.session.state.characters[character_id]
        target = max(EDGE, min(1 - EDGE, float(x)))
        ops = self._clear_path_for(character_id, target, now)
        target = self._free_x(character_id, target)
        if abs(target - state.x) < 0.02:
            return True, "already there", ops
        ops += self._glide(character_id, target, now, style)
        label = self.session.world.zone_label(zone) if zone else "a new spot"
        self.session.state.record(
            "moved", f"{self._name(character_id)} moved to {label}{why}", character_id
        )
        return True, "", ops

    def home(
        self, character_id: str, why: str = ""
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        state = self.session.state.characters.get(character_id)
        if not state:
            return False, "unknown character", []
        return self.move_to(character_id, x=state.home_x, why=why)

    def gesture(
        self, character_id: str, kind: str
    ) -> tuple[bool, str, list[dict[str, Any]]]:
        """Hop, jump or dance in place (whole-model motion, no repositioning)."""
        if kind not in GESTURE_SECONDS:
            return False, f"cannot {kind}", []
        if kind == "dance" and self.session.show.engine.playing:
            return False, "a game is on the board, so no dancing until it ends", []
        state = self.session.state.characters.get(character_id)
        if not state or not state.is_available():
            return False, "she is not on stage right now", []
        state.moving_until = self.clock() + GESTURE_SECONDS[kind]
        return True, "", [{"op": "stage", "character": character_id, "move": kind}]

    def all_home(self) -> list[dict[str, Any]]:
        now = self.clock()
        ops: list[dict[str, Any]] = []
        for state in self.session.state.characters.values():
            ops += self._glide(state.id, state.home_x, now, style="walk")
        return ops

    def on_game_started(self, event: ev.Event):
        return self.all_home()

    def zone_names(self) -> list[str]:
        zones = self.session.world.zones()
        named = [z for z in zones if z not in ZONE_ORDER]
        return list(ZONE_ORDER) + named

    def describe(self) -> dict[str, Any]:
        return {
            "positions": {
                cid: {"x": round(c.x, 3), "zone": c.zone}
                for cid, c in self.session.state.characters.items()
            },
            "moves": self.moves,
            "refused": self.refused,
        }
