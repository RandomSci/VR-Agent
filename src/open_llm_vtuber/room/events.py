"""Typed in-process events that coordinate the show.

One event (for example ANSWER_CORRECT) can drive several systems at once:
the Attention Director turns heads, the Action Director plays a reaction,
the Camera Director frames it and the renderer plays a sound. Handlers are
plain synchronous functions that return renderer ops, which keeps every rule
deterministic and easy to test. Nothing here calls an LLM or TTS.
"""

from __future__ import annotations

import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Optional

from loguru import logger

# Conversation and world
VIEWER_MESSAGE = "VIEWER_MESSAGE"
VIEWER_ADDRESSED = (
    "VIEWER_ADDRESSED"  # data: targets (list of character ids or ["all"])
)
SPEECH_STARTED = "SPEECH_STARTED"  # data: character, addressee, seconds
SPEECH_ENDED = "SPEECH_ENDED"  # data: character
INTERACTION_STARTED = "INTERACTION_STARTED"
INTERACTION_FINISHED = "INTERACTION_FINISHED"
REACTION = "REACTION"  # data: character, reaction
ACTION_STARTED = "ACTION_STARTED"  # data: character, action
OBJECT_APPEARED = "OBJECT_APPEARED"  # data: id, source, seconds
OBJECT_REMOVED = "OBJECT_REMOVED"  # data: id

# Games
GAME_STARTED = "GAME_STARTED"
GAME_PAUSED = "GAME_PAUSED"
GAME_RESUMED = "GAME_RESUMED"
ROUND_STARTED = "ROUND_STARTED"
QUESTION_SHOWN = "QUESTION_SHOWN"
CHARACTER_TURN = "CHARACTER_TURN"  # data: player
CHARACTER_ANSWERED = "CHARACTER_ANSWERED"  # data: player, answer, correct
VIEWER_ANSWERED = "VIEWER_ANSWERED"  # data: username, correct
ANSWER_CORRECT = "ANSWER_CORRECT"  # data: player ("viewers" or a character id)
ANSWER_WRONG = "ANSWER_WRONG"  # data: player
SCORE_CHANGED = "SCORE_CHANGED"
ROUND_FINISHED = "ROUND_FINISHED"
GAME_FINISHED = "GAME_FINISHED"  # data: winner, loser (ids or None)
GAME_STOPPED = "GAME_STOPPED"
MOVE_MADE = "MOVE_MADE"  # data: player, move
VIEWER_TIMEOUT = "VIEWER_TIMEOUT"  # data: game, round

ALL_EVENTS = frozenset(
    v
    for k, v in dict(globals()).items()
    if k.isupper() and isinstance(v, str) and v == k
)

Ops = list[dict[str, Any]]
Handler = Callable[["Event"], Optional[Ops]]


@dataclass(frozen=True)
class Event:
    name: str
    data: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


class EventBus:
    """Synchronous publish and subscribe. Handlers return renderer ops."""

    def __init__(self, log_size: int = 300):
        self._handlers: dict[str, list[Handler]] = {}
        self.log: Deque[dict[str, Any]] = deque(maxlen=log_size)
        self.counts: Counter[str] = Counter()

    def subscribe(self, name: str, handler: Handler) -> None:
        if name != "*" and name not in ALL_EVENTS:
            raise ValueError(f"unknown event {name}")
        self._handlers.setdefault(name, []).append(handler)

    def emit(self, name: str, **data: Any) -> Ops:
        if name not in ALL_EVENTS:
            raise ValueError(f"unknown event {name}")
        event = Event(name, data)
        self.counts[name] += 1
        self.log.append({"event": name, "at": round(event.at, 3), **_loggable(data)})
        ops: Ops = []
        for handler in self._handlers.get(name, []) + self._handlers.get("*", []):
            try:
                ops.extend(op for op in (handler(event) or []) if op)
            except Exception as exc:  # one broken rule never stops the show
                logger.error(f"VR Room: handler for {name} failed: {exc}")
        return ops

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        return list(self.log)[-limit:]


def _loggable(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value if not isinstance(value, str) else value[:80]
        elif isinstance(value, (list, tuple)):
            out[key] = [str(v)[:40] for v in value[:6]]
    return out
