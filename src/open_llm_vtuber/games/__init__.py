"""Visual games played inside the VR Agent room.

Pure logic: nothing in this package imports Live2D, YouTube, the room, the
LLM or TTS. Games emit events, template line requests and a view model the
in-room Game Board renders.
"""

from .base import GameEvent, LineRequest, PlayerSpec, StepResult
from .commands import parse_command
from .engine import EngineSettings, GameEngine, Outcome
from .registry import GameRegistry

__all__ = [
    "EngineSettings",
    "GameEngine",
    "GameEvent",
    "GameRegistry",
    "LineRequest",
    "Outcome",
    "PlayerSpec",
    "StepResult",
    "parse_command",
]
