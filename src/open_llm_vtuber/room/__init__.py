"""VR Agent Room: several characters, directors and games in one livestream scene.

The single-character livestream (``vr_agent`` package and ``/?mode=live``) does
not depend on anything here and keeps working when the room is disabled.
"""

from .profiles import CharacterProfile, RoomConfig, load_room

__all__ = ["CharacterProfile", "RoomConfig", "load_room"]
