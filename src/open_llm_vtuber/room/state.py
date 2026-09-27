"""Shared room state: ONE authoritative world.

RoomState is the single source of truth. The directors (world, stage,
games, adventure) write to it, the renderer draws it, and the Conversation
Director gives the characters a compact slice of it (room/awareness.py).
Nothing on screen should exist that the state does not know about, and the
characters are never told about anything the screen does not show.

Everything here is small and bounded: a handful of characters, at most a
few dozen objects, a short causal history.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Optional

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


@dataclass(frozen=True)
class AttentionTarget:
    """Where a character looks. Serialised as VIEWER, CHARACTER:luna, OBJECT:game_board ..."""

    kind: str  # VIEWER | CAMERA | NEUTRAL | GAME | CHARACTER | OBJECT
    ref: str = ""

    SIMPLE = (
        "VIEWER",
        "CAMERA",
        "NEUTRAL",
        "GAME",
        "CHAT",
        "LEFT",
        "RIGHT",
        "UP",
        "DOWN",
    )
    WITH_REF = ("CHARACTER", "OBJECT")

    @classmethod
    def parse(cls, value: Any) -> "AttentionTarget":
        text = str(value or "").strip()
        kind, _, ref = text.partition(":")
        kind = kind.upper()
        if kind in cls.SIMPLE and not ref:
            return cls(kind)
        if kind in cls.WITH_REF and _ID_RE.match(ref.lower()):
            return cls(kind, ref.lower())
        raise ValueError(f"invalid attention target '{text[:40]}'")

    @classmethod
    def character(cls, character_id: str) -> "AttentionTarget":
        return cls("CHARACTER", character_id)

    @classmethod
    def object(cls, object_id: str) -> "AttentionTarget":
        return cls("OBJECT", object_id)

    def __str__(self) -> str:
        return f"{self.kind}:{self.ref}" if self.ref else self.kind


VIEWER = AttentionTarget("VIEWER")
CAMERA = AttentionTarget("CAMERA")
NEUTRAL = AttentionTarget("NEUTRAL")
GAME = AttentionTarget("GAME")


@dataclass
class CharacterState:
    id: str
    name: str
    available: bool = True
    speaking: bool = False
    attention: AttentionTarget = VIEWER
    attention_source: str = "ambient"
    attention_until: float = 0.0
    expression: Optional[str] = None
    action: Optional[str] = None
    game_role: Optional[str] = None
    recent_dialogue: Deque[str] = field(default_factory=lambda: deque(maxlen=4))
    failures: int = 0
    cooldown_until: float = 0.0
    spoken_turns: int = 0
    # Where she stands (fraction of the stage width) and what she is doing.
    x: float = 0.5
    zone: str = "center"
    home_x: float = 0.5
    activity: str = ""
    moving_until: float = 0.0

    def is_available(self, now: Optional[float] = None) -> bool:
        return self.available and (now or time.time()) >= self.cooldown_until

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "available": self.is_available(),
            "speaking": self.speaking,
            "attention": str(self.attention),
            "attention_source": self.attention_source,
            "expression": self.expression,
            "action": self.action,
            "game_role": self.game_role,
            "failures": self.failures,
            "spoken_turns": self.spoken_turns,
            "x": round(self.x, 3),
            "zone": self.zone,
            "activity": self.activity,
        }


@dataclass
class RoomObject:
    """Something that exists in the world. The renderer draws ``type`` at
    (x, y); the characters can look at it and are told it is there while
    ``visible``. Objects without a type are invisible anchors (look targets).
    """

    id: str
    x: float
    y: float
    width: float = 0.1
    height: float = 0.1
    visible: bool = True
    expires_at: float = 0.0  # 0 means permanent
    type: str = ""
    label: str = ""
    zone: str = ""
    state: str = ""
    interactable: bool = False
    scene: str = ""
    source: str = ""  # who or what made it appear
    by_motion: bool = False  # drawn by a Live2D motion, not by the renderer

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "visible": self.visible,
            "type": self.type,
            "label": self.label,
            "zone": self.zone,
            "state": self.state,
            "interactable": self.interactable,
            "by_motion": self.by_motion,
        }


@dataclass
class WorldEvent:
    """One entry of the recent causal history ("the frog croaked on the right",
    "Mika's spell made the frog vanish")."""

    kind: str
    text: str
    subjects: tuple[str, ...] = ()
    at: float = 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "text": self.text,
            "subjects": list(self.subjects),
            "at": round(self.at, 2),
        }


@dataclass
class SceneState:
    """Where the room is right now. The plain stream room by default; the
    Adventure Director swaps in forest trails, riversides and ruins."""

    id: str = "room"
    name: str = "the stream room"
    env: str = "room"
    region: str = ""
    time: str = "night"
    weather: str = "clear"
    description: str = ""
    zones: dict[str, float] = field(default_factory=dict)
    zone_labels: dict[str, str] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "env": self.env,
            "region": self.region,
            "time": self.time,
            "weather": self.weather,
            "zones": dict(self.zones),
            "zone_labels": dict(self.zone_labels),
        }


@dataclass
class RoomLine:
    speaker: str  # character id, or "@viewer name"
    text: str
    at: float


@dataclass
class RoomState:
    characters: dict[str, CharacterState] = field(default_factory=dict)
    objects: dict[str, RoomObject] = field(default_factory=dict)
    current_speaker: Optional[str] = None
    previous_speaker: Optional[str] = None
    current_topic: str = ""
    recent_lines: Deque[RoomLine] = field(default_factory=lambda: deque(maxlen=12))
    last_viewer_at: float = 0.0
    active_interaction: Optional[str] = None
    scene: SceneState = field(default_factory=SceneState)
    history: Deque[WorldEvent] = field(default_factory=lambda: deque(maxlen=16))
    MAX_OBJECTS = 40

    def record(
        self, kind: str, text: str, *subjects: str, at: Optional[float] = None
    ) -> None:
        """Add one line to the bounded causal history."""
        text = " ".join(str(text).split())[:140]
        if text:
            self.history.append(
                WorldEvent(
                    kind, text, tuple(s for s in subjects if s), at or time.time()
                )
            )

    def recent_history(
        self, within_seconds: float = 900.0, limit: int = 6
    ) -> list[WorldEvent]:
        cutoff = time.time() - within_seconds
        return [e for e in self.history if e.at >= cutoff][-limit:]

    def visible_objects(self) -> list[RoomObject]:
        return [
            o
            for o in self.objects.values()
            if o.visible and o.type and o.type != "game_board"
        ]

    def add_line(self, speaker: str, text: str) -> None:
        text = " ".join(str(text).split())[:200]
        if text:
            self.recent_lines.append(RoomLine(speaker, text, time.time()))

    def available_characters(self) -> list[str]:
        now = time.time()
        return [c.id for c in self.characters.values() if c.is_available(now)]

    def snapshot(self) -> dict[str, Any]:
        return {
            "characters": {k: v.snapshot() for k, v in self.characters.items()},
            "objects": {k: v.snapshot() for k, v in self.objects.items() if v.visible},
            "scene": self.scene.snapshot(),
            "history": [e.snapshot() for e in list(self.history)[-8:]],
            "current_speaker": self.current_speaker,
            "previous_speaker": self.previous_speaker,
            "current_topic": self.current_topic,
            "recent_lines": [
                {"speaker": line.speaker, "text": line.text}
                for line in self.recent_lines
            ],
            "active_interaction": self.active_interaction,
        }
