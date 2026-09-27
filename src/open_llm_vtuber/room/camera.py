"""Camera Director: frames the room from events, with cooldowns. No LLM.

Shots are names only (wide, two_shot, focus, closeup, board). The room page
resolves them from the cast layout and eases between them, so no numbers
from chat ever reach the renderer.

* A game makes the board framing the resting shot; the room otherwise rests
  on the wide shot.
* Automatic moves have a cooldown so the camera never keeps zooming.
* Viewers can ask for a zoom ("zoom in on Luna"); that has its own cooldown,
  holds briefly and returns to the resting shot.
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any, Optional

from . import events as ev

if TYPE_CHECKING:  # pragma: no cover
    from .session import RoomSession

SHOTS = ("wide", "two_shot", "focus", "closeup", "board")
GAME_MOVE_GAP_SECONDS = 4.0

ZOOM_OUT_RE = re.compile(
    r"\bzoom(?:ed)?\s*out\b|\bwide shot\b|\bshow (?:both|everyone|the room)\b", re.I
)
ZOOM_IN_RE = re.compile(
    r"\bzoom(?:\s*in)?\b|\bclose[- ]?up\b|\bcloser\b|\bshow (?:me |us )?(?:your|her|their|ur) face\b",
    re.I,
)


def camera_request(text: str) -> Optional[str]:
    """'in', 'out' or None. Short messages only, so chat about cameras is not a command."""
    if not text or len(text.split()) > 10:
        return None
    if ZOOM_OUT_RE.search(text):
        return "out"
    if ZOOM_IN_RE.search(text):
        return "in"
    return None


class CameraDirector:
    def __init__(self, session: "RoomSession", clock=time.time):
        self.session = session
        self.clock = clock
        self.base = "wide"
        self.current = "wide"
        self.current_target: Optional[str] = None
        self.last_auto_at = -1e9
        self.last_game_move_at = -1e9
        self.last_request_at = -1e9
        self.moves = 0
        bus = session.bus
        bus.subscribe(ev.GAME_STARTED, self.on_game_started)
        bus.subscribe(ev.QUESTION_SHOWN, self.on_question_shown)
        bus.subscribe(ev.CHARACTER_TURN, self.on_character_turn)
        bus.subscribe(ev.GAME_FINISHED, self.on_game_finished)
        bus.subscribe(ev.GAME_STOPPED, self.on_game_stopped)
        bus.subscribe(ev.INTERACTION_STARTED, self.on_interaction_started)
        bus.subscribe(ev.SPEECH_STARTED, self.on_speech_started)
        bus.subscribe(ev.INTERACTION_FINISHED, self.on_interaction_finished)

    @property
    def settings(self):
        return self.session.room.camera

    def _op(
        self, shot: str, target: Optional[str] = None, hold: float = 0.0
    ) -> Optional[dict[str, Any]]:
        if not self.settings.enabled or shot not in SHOTS:
            return None
        if target is not None and target not in self.session.state.characters:
            return None
        if shot == self.current and target == self.current_target and hold <= 0:
            return None
        self.current = shot if hold <= 0 else self.base
        self.current_target = target if hold <= 0 else None
        self.moves += 1
        return {
            "op": "camera",
            "shot": shot,
            "target": target,
            "hold_ms": int(max(0.0, hold) * 1000),
            "return_to": self.base,
        }

    def _auto_ok(self) -> bool:
        return self.clock() - self.last_auto_at >= self.settings.auto_cooldown_seconds

    def _game_ok(self) -> bool:
        return self.clock() - self.last_game_move_at >= GAME_MOVE_GAP_SECONDS

    # ------------------------------------------------------------------
    # games
    # ------------------------------------------------------------------
    def on_game_started(self, event: ev.Event):
        self.base = "board"
        self.last_game_move_at = self.clock()
        return [self._op("board")]

    def on_question_shown(self, event: ev.Event):
        self.last_game_move_at = self.clock()
        return [self._op("board")]

    def on_character_turn(self, event: ev.Event):
        player = event.get("player")
        if not self._game_ok() or player not in self.session.state.characters:
            return []
        engine = getattr(getattr(self.session, "show", None), "engine", None)
        active = getattr(engine, "active", None)
        if active is not None and getattr(active.info, "renderer", "") in (
            "grid",
            "rps",
        ):
            # Board games move every few seconds: zooming to the mover each time
            # hides the board and the other girl. Keep the board framing.
            self.last_game_move_at = self.clock()
            return []
        self.last_game_move_at = self.clock()
        return [self._op("focus", player, hold=5.0)]

    def on_game_finished(self, event: ev.Event):
        winner = event.get("winner")
        self.last_game_move_at = self.clock()
        if winner in self.session.state.characters:
            return [self._op("closeup", winner, hold=3.5)]
        return [self._op("board")]

    def on_game_stopped(self, event: ev.Event):
        return self.game_cleared()

    def game_cleared(self) -> list[dict[str, Any]]:
        if self.base == "wide" and self.current == "wide":
            return []
        self.base = "wide"
        return [self._op("wide")]

    # ------------------------------------------------------------------
    # conversation
    # ------------------------------------------------------------------
    def on_interaction_started(self, event: ev.Event):
        speakers = [
            s
            for s in (event.get("speakers") or [])
            if s in self.session.state.characters
        ]
        if len(set(speakers)) >= 2 and self.base == "wide" and self._auto_ok():
            self.last_auto_at = self.clock()
            self._multi = True
            return [self._op("two_shot")]
        self._multi = False
        return []

    def on_speech_started(self, event: ev.Event):
        speaker = event.get("character")
        if (
            not self.settings.auto_focus
            or getattr(self, "_multi", False)
            or self.base != "wide"
            or speaker not in self.session.state.characters
            or not self._auto_ok()
        ):
            return []
        self.last_auto_at = self.clock()
        seconds = float(event.get("seconds") or 5.0)
        return [self._op("focus", speaker, hold=min(8.0, max(3.0, seconds)))]

    def on_interaction_finished(self, event: ev.Event):
        self._multi = False
        if self.current != self.base:
            return [self._op(self.base)]
        return []

    # ------------------------------------------------------------------
    # viewer requests
    # ------------------------------------------------------------------
    def request(self, kind: str, target: Optional[str]) -> list[dict[str, Any]]:
        now = self.clock()
        if (
            not self.settings.enabled
            or now - self.last_request_at < self.settings.zoom_request_cooldown_seconds
        ):
            return []
        self.last_request_at = now
        self.last_auto_at = now  # no automatic move right after a viewer's request
        if kind == "out":
            return [
                self._op(self.base if self.base != "wide" else "wide", hold=0) or {}
            ]
        if target not in self.session.state.characters:
            return []
        return [self._op("closeup", target, hold=self.settings.zoom_hold_seconds)]

    def status(self) -> dict[str, Any]:
        return {
            "base": self.base,
            "current": self.current,
            "target": self.current_target,
            "moves": self.moves,
        }
