"""Action Director: semantic reactions mapped onto real registry actions.

"celebrate", "lose", "surprised" ... are translated per character using the
``reactions`` table in that character's yaml. Only actions the model really
has (validated when the room loads) can be chosen. Per-character cooldowns
stop reaction spam and big motions keep a minimum gap. No LLM calls.
"""

from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING, Any, Optional

from . import events as ev

if TYPE_CHECKING:  # pragma: no cover
    from .session import RoomSession

REACTION_COOLDOWN_SECONDS = 3.0


class ActionDirector:
    def __init__(
        self,
        session: "RoomSession",
        rng: Optional[random.Random] = None,
        clock=time.time,
    ):
        self.session = session
        self.rng = rng or random.Random()
        self.clock = clock
        self._last_reaction: dict[str, float] = {}
        self._last_large: dict[str, float] = {}
        bus = session.bus
        bus.subscribe(ev.REACTION, self.on_reaction)
        bus.subscribe(ev.ANSWER_CORRECT, self.on_answer_correct)
        bus.subscribe(ev.ANSWER_WRONG, self.on_answer_wrong)
        bus.subscribe(ev.GAME_FINISHED, self.on_game_finished)
        bus.subscribe(ev.OBJECT_APPEARED, self.on_object_appeared)

    # ------------------------------------------------------------------
    def choose(
        self, character_id: str, reaction: str, allow_motion: bool = True
    ) -> Optional[str]:
        profile = self.session.room.get(character_id)
        if not profile or not profile.capabilities:
            return None
        caps = profile.capabilities
        now = self.clock()
        options = []
        for name in profile.reactions.get(reaction, ()):
            action = caps.get(name)
            if not action:
                continue
            if action.kind == "motion" and not allow_motion:
                continue
            if name in caps.large_motion_actions and (
                now - self._last_large.get(character_id, 0.0)
                < caps.large_motion_min_gap_seconds
            ):
                continue
            options.append(name)
        if not options:
            return None
        # Prefer the first listed action most of the time, keep some variety.
        return options[0] if self.rng.random() < 0.6 else self.rng.choice(options)

    def react(
        self,
        character_id: str,
        reaction: str,
        delay: float = 0.0,
        allow_motion: bool = True,
        force: bool = False,
    ) -> Optional[dict[str, Any]]:
        character = self.session.state.characters.get(character_id)
        if not character or not character.is_available():
            return None
        now = self.clock()
        if (
            not force
            and now - self._last_reaction.get(character_id, 0.0)
            < REACTION_COOLDOWN_SECONDS
        ):
            return None
        name = self.choose(character_id, reaction, allow_motion)
        if not name:
            return None
        op = self.session.action_op(character_id, name, delay_seconds=delay)
        if op:
            self._last_reaction[character_id] = now
            profile = self.session.room.get(character_id)
            if profile and name in profile.capabilities.large_motion_actions:
                self._last_large[character_id] = now
        return op

    def _others(self, character_id: Optional[str]) -> list[str]:
        return [
            c for c in self.session.state.available_characters() if c != character_id
        ]

    def _is_character(self, player: Any) -> bool:
        return isinstance(player, str) and player in self.session.state.characters

    # ------------------------------------------------------------------
    def on_reaction(self, event: ev.Event):
        character = event.get("character")
        reaction = event.get("reaction")
        if not isinstance(reaction, str):
            return []
        return [self.react(character, reaction, float(event.get("delay") or 0.0))]

    def on_answer_correct(self, event: ev.Event):
        player = event.get("player")
        if not self._is_character(player):
            return [
                self.react(c, "surprised", 0.3 + 0.2 * i, allow_motion=False)
                for i, c in enumerate(self._others(None))
            ]
        ops = [self.react(player, "happy", 0.2, allow_motion=False, force=True)]
        ops += [
            self.react(c, "surprised", 0.6, allow_motion=False)
            for c in self._others(player)
        ]
        return ops

    def on_answer_wrong(self, event: ev.Event):
        player = event.get("player")
        if not self._is_character(player):
            return []
        ops = [self.react(player, "lose", 0.2, allow_motion=False, force=True)]
        ops += [
            self.react(c, "suspicious", 0.7, allow_motion=False)
            for c in self._others(player)
        ]
        return ops

    def on_game_finished(self, event: ev.Event):
        winner, loser = event.get("winner"), event.get("loser")
        ops = []
        if self._is_character(winner):
            ops.append(self.react(winner, "celebrate", 0.3, force=True))
        if self._is_character(loser):
            ops.append(self.react(loser, "lose", 0.8, force=True))
        return ops

    def on_object_appeared(self, event: ev.Event):
        source = event.get("source")
        delay = min(9.0, float(event.get("after") or 0.0) + 0.8)
        return [
            self.react(c, "surprised", delay, allow_motion=False)
            for c in self._others(source)
        ]
