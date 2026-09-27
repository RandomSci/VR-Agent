"""Persistent Adventure World: Mika and Luna's autonomous journey.

Everything in this package is deterministic and local. It never calls an LLM
or a TTS engine: pacing, events, weather, dialogue selection and progression
are ordinary state machines driven by the room tick. Viewer conversation (the
Conversation Director) and games (the Game Engine) temporarily take the stage
and the adventure resumes afterwards.
"""

from .content import AdventureLibrary, load_library
from .director import AdventureDirector
from .state import AdventureState, StateStore

__all__ = [
    "AdventureDirector",
    "AdventureLibrary",
    "AdventureState",
    "StateStore",
    "load_library",
]
