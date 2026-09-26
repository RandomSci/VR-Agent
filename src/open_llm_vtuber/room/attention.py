"""Attention Director: where each character looks, decided by local rules.

Listens to room and game events and sets directed attention targets with a
hold time. When a directed target expires the renderer returns the character
to local ambient attention (glances, looking at the viewer). No LLM calls.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING, Any, Optional

from . import events as ev
from .state import AttentionTarget, GAME, VIEWER

if TYPE_CHECKING:  # pragma: no cover
    from .session import RoomSession


class AttentionDirector:
    def __init__(self, session: "RoomSession", rng: Optional[random.Random] = None):
        self.session = session
        self.rng = rng or random.Random()
        bus = session.bus
        bus.subscribe(ev.SPEECH_STARTED, self.on_speech_started)
        bus.subscribe(ev.VIEWER_ADDRESSED, self.on_viewer_addressed)
        bus.subscribe(ev.GAME_STARTED, self.on_game_started)
        bus.subscribe(ev.QUESTION_SHOWN, self.on_question_shown)
        bus.subscribe(ev.CHARACTER_TURN, self.on_character_turn)
        bus.subscribe(ev.CHARACTER_ANSWERED, self.on_character_answered)
        bus.subscribe(ev.VIEWER_ANSWERED, self.on_viewer_answered)
        bus.subscribe(ev.ANSWER_CORRECT, self.on_answer_correct)
        bus.subscribe(ev.GAME_FINISHED, self.on_game_finished)
        bus.subscribe(ev.OBJECT_APPEARED, self.on_object_appeared)

    # ------------------------------------------------------------------
    def _ids(self) -> list[str]:
        return self.session.state.available_characters()

    def _look(
        self,
        character: str,
        target: AttentionTarget | str,
        hold: float,
        delay: float = 0.0,
    ) -> Optional[dict[str, Any]]:
        return self.session.attention_op(
            character,
            target,
            source="attention",
            hold_seconds=hold,
            delay_seconds=delay,
        )

    def _react_delay(self) -> float:
        return round(self.rng.uniform(0.2, 0.7), 2)

    def _is_character(self, player: Any) -> bool:
        return isinstance(player, str) and player in self.session.state.characters

    # ------------------------------------------------------------------
    def on_speech_started(self, event: ev.Event):
        speaker = event.get("character")
        if not self._is_character(speaker):
            return []
        seconds = float(event.get("seconds") or 6.0)
        addressee = event.get("addressee")
        ops = []
        if self._is_character(addressee) and addressee != speaker:
            ops.append(
                self._look(speaker, AttentionTarget.character(addressee), seconds)
            )
            ops.append(
                self._look(
                    addressee,
                    AttentionTarget.character(speaker),
                    seconds,
                    self._react_delay(),
                )
            )
            listeners = [c for c in self._ids() if c not in (speaker, addressee)]
        else:
            ops.append(self._look(speaker, VIEWER, seconds))
            listeners = [c for c in self._ids() if c != speaker]
        for other in listeners:
            ops.append(
                self._look(
                    other,
                    AttentionTarget.character(speaker),
                    seconds,
                    self._react_delay(),
                )
            )
        return ops

    def on_viewer_addressed(self, event: ev.Event):
        targets = event.get("targets") or []
        ids = (
            self._ids()
            if "all" in targets
            else [t for t in targets if t in self._ids()]
        )
        return [
            self._look(c, VIEWER, 3.0, self._react_delay() if i else 0.0)
            for i, c in enumerate(ids)
        ]

    def on_game_started(self, event: ev.Event):
        return [self._look(c, GAME, 3.0, 0.15 * i) for i, c in enumerate(self._ids())]

    def on_question_shown(self, event: ev.Event):
        return [
            self._look(c, AttentionTarget.object("game_board"), 4.0, 0.2 * i)
            for i, c in enumerate(self._ids())
        ]

    def on_character_turn(self, event: ev.Event):
        player = event.get("player")
        if not self._is_character(player):
            return []
        ops = [self._look(player, GAME, 3.0)]
        ops += [
            self._look(c, AttentionTarget.character(player), 3.0, self._react_delay())
            for c in self._ids()
            if c != player
        ]
        return ops

    def on_character_answered(self, event: ev.Event):
        player = event.get("player")
        if not self._is_character(player):
            return []
        return [
            self._look(c, AttentionTarget.character(player), 2.5, self._react_delay())
            for c in self._ids()
            if c != player
        ]

    def on_viewer_answered(self, event: ev.Event):
        return [self._look(c, VIEWER, 2.5, 0.2 * i) for i, c in enumerate(self._ids())]

    def on_answer_correct(self, event: ev.Event):
        player = event.get("player")
        if not self._is_character(player):
            # Viewers got it: everyone looks at the viewer (camera).
            return [
                self._look(c, VIEWER, 2.5, 0.15 * i) for i, c in enumerate(self._ids())
            ]
        ops = [self._look(player, VIEWER, 2.5, 0.3)]
        ops += [
            self._look(c, AttentionTarget.character(player), 2.5, self._react_delay())
            for c in self._ids()
            if c != player
        ]
        return ops

    def on_game_finished(self, event: ev.Event):
        winner = event.get("winner")
        if not self._is_character(winner):
            return [
                self._look(c, VIEWER, 3.0, 0.2 * i) for i, c in enumerate(self._ids())
            ]
        ops = [self._look(winner, VIEWER, 4.0)]
        ops += [
            self._look(c, AttentionTarget.character(winner), 4.0, self._react_delay())
            for c in self._ids()
            if c != winner
        ]
        return ops

    def on_object_appeared(self, event: ev.Event):
        object_id = event.get("id")
        if object_id not in self.session.state.objects:
            return []
        seconds = float(event.get("seconds") or 3.0)
        after = float(event.get("after") or 0.0)
        source = event.get("source")
        ops = []
        for c in self._ids():
            delay = after + (
                0.1 if c == source else round(self.rng.uniform(0.3, 0.9), 2)
            )
            ops.append(
                self._look(
                    c, AttentionTarget.object(object_id), min(seconds, 4.0), delay
                )
            )
        return ops
