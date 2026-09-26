"""Shared room state: characters, attention targets and room objects.

RoomState is the single source of truth the directors write to and the
renderer draws from. It holds only small, bounded data.
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

    SIMPLE = ("VIEWER", "CAMERA", "NEUTRAL", "GAME")
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
        }


@dataclass
class RoomObject:
    id: str
    x: float
    y: float
    width: float = 0.1
    height: float = 0.1
    visible: bool = True
    expires_at: float = 0.0  # 0 means permanent

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "visible": self.visible,
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
            "current_speaker": self.current_speaker,
            "previous_speaker": self.previous_speaker,
            "current_topic": self.current_topic,
            "recent_lines": [
                {"speaker": line.speaker, "text": line.text}
                for line in self.recent_lines
            ],
            "active_interaction": self.active_interaction,
        }
