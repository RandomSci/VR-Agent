"""Game plugin interface.

A game is pure logic. It knows nothing about Live2D, YouTube, the DOM, the
LLM or TTS. It receives players, typed commands, viewer messages (already
sanitised) and the current time, and it produces:

* ``GameEvent`` objects the room turns into looks, reactions, camera and sound
* ``LineRequest`` objects when a character should say something (the room
  decides whether speech is allowed and which voice to use)
* a view model (``view()``) the in-room Game Board renders

Rules, scores, timers and correct answers are decided here, deterministically.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Optional, Union

# ---------------------------------------------------------------------------
# Event names (mirrored by the room's event bus)
# ---------------------------------------------------------------------------
GAME_STARTED = "GAME_STARTED"
GAME_PAUSED = "GAME_PAUSED"
GAME_RESUMED = "GAME_RESUMED"
ROUND_STARTED = "ROUND_STARTED"
QUESTION_SHOWN = "QUESTION_SHOWN"
CHARACTER_TURN = "CHARACTER_TURN"
CHARACTER_ANSWERED = "CHARACTER_ANSWERED"
VIEWER_ANSWERED = "VIEWER_ANSWERED"
ANSWER_CORRECT = "ANSWER_CORRECT"
ANSWER_WRONG = "ANSWER_WRONG"
SCORE_CHANGED = "SCORE_CHANGED"
ROUND_FINISHED = "ROUND_FINISHED"
GAME_FINISHED = "GAME_FINISHED"
GAME_STOPPED = "GAME_STOPPED"

VIEWERS = "viewers"  # the shared player id for chat


@dataclass(frozen=True)
class GameEvent:
    name: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LineRequest:
    """A character should say a line. ``kind`` picks a template pool."""

    player: str
    kind: str  # answer, correct, wrong, win, lose, viewer_first, bored, intro
    values: dict[str, str] = field(default_factory=dict)
    blocking: bool = False  # the game waits for character_done() before moving on


@dataclass(frozen=True)
class PlayerSpec:
    id: str
    name: str
    skill: dict[str, float] = field(
        default_factory=dict
    )  # difficulty -> chance correct


@dataclass(frozen=True)
class GameInfo:
    id: str
    display_name: str
    description: str = ""
    players: int = 2
    viewer_participation: bool = True
    enabled: bool = True
    categories: tuple[str, ...] = ()
    difficulties: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    how_to_play: str = ""

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "how_to_play": self.how_to_play,
            "players": self.players,
            "viewer_participation": self.viewer_participation,
            "enabled": self.enabled,
            "categories": list(self.categories),
            "difficulties": list(self.difficulties),
        }


@dataclass
class StepResult:
    events: list[GameEvent] = field(default_factory=list)
    lines: list[LineRequest] = field(default_factory=list)

    def extend(self, other: "StepResult") -> "StepResult":
        self.events.extend(other.events)
        self.lines.extend(other.lines)
        return self

    def event(self, name: str, **data: Any) -> None:
        self.events.append(GameEvent(name, data))

    def line(
        self, player: str, kind: str, blocking: bool = False, **values: str
    ) -> None:
        self.lines.append(LineRequest(player, kind, dict(values), blocking))


# ---------------------------------------------------------------------------
# Typed commands. Chat text becomes one of these or nothing.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StartGame:
    game_id: Optional[str] = None  # None means the default game
    requested_name: Optional[str] = None  # recognised but not installed, e.g. "chess"


@dataclass(frozen=True)
class StopGame:
    pass


@dataclass(frozen=True)
class NextRound:
    pass


@dataclass(frozen=True)
class ChangeGame:
    game_id: Optional[str] = None
    requested_name: Optional[str] = None


@dataclass(frozen=True)
class ListGames:
    pass


@dataclass(frozen=True)
class HowToPlay:
    game_id: Optional[str] = None  # None means the offered or default game


@dataclass(frozen=True)
class SetDifficulty:
    level: str  # easy, medium, hard, or harder / easier (relative)


@dataclass(frozen=True)
class SetCategory:
    category: str


@dataclass(frozen=True)
class SetFirstPlayer:
    player: str


Command = Union[
    StartGame,
    StopGame,
    NextRound,
    ChangeGame,
    ListGames,
    HowToPlay,
    SetDifficulty,
    SetCategory,
    SetFirstPlayer,
]
GAME_ONLY_COMMANDS = (NextRound, SetDifficulty, SetCategory, SetFirstPlayer)


# ---------------------------------------------------------------------------
# Game interface
# ---------------------------------------------------------------------------
class Game(abc.ABC):
    """One running game. The engine owns the lifecycle."""

    info: GameInfo

    @abc.abstractmethod
    def start(self, players: list[PlayerSpec], now: float) -> StepResult: ...

    @abc.abstractmethod
    def tick(self, now: float) -> StepResult: ...

    @abc.abstractmethod
    def handle_viewer_message(
        self, username: str, text: str, now: float
    ) -> tuple[bool, StepResult]:
        """Returns (consumed, result). Consumed messages never reach the LLM."""

    @abc.abstractmethod
    def character_done(self, player: str, now: float) -> StepResult:
        """The show finished a blocking line for this player."""

    def handle_command(
        self, command: Command, now: float
    ) -> tuple[bool, str, StepResult]:
        """Returns (accepted, reply_key, result) for game-specific commands."""
        return False, "unsupported", StepResult()

    @abc.abstractmethod
    def pause(self, now: float, reason: str) -> StepResult: ...

    @abc.abstractmethod
    def resume(self, now: float) -> StepResult: ...

    @abc.abstractmethod
    def stop(self, now: float, reason: str) -> StepResult: ...

    @property
    @abc.abstractmethod
    def finished(self) -> bool: ...

    @property
    @abc.abstractmethod
    def paused(self) -> bool: ...

    @abc.abstractmethod
    def view(self, now: float) -> dict[str, Any]:
        """View model for the Game Board. JSON serialisable, no free HTML."""

    @abc.abstractmethod
    def state(self) -> dict[str, Any]:
        """Truth for monitoring and tests."""

    def at_checkpoint(self) -> bool:
        """True when a conversation reply can be slotted in without breaking a turn."""
        return True

    def switch_suggestion_available(self) -> bool:
        return False
