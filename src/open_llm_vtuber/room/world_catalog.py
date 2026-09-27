"""What can exist in the world: stage zones and object types.

Plain data shared by the backend (authoritative state, awareness, requests)
and mirrored by the renderer (frontend/vr-agent/room-world.js draws each
object type). Keep ids stable: they are used in saved adventure state.
"""

from __future__ import annotations

from dataclasses import dataclass

# Generic stage zones, as fractions of the 16:9 stage width. Every scene has
# them; scenes may add named zones ("pond", "campfire") on top.
ZONE_ORDER = ("far_left", "left", "center", "right", "far_right")
DEFAULT_ZONES = {
    "far_left": 0.13,
    "left": 0.28,
    "center": 0.5,
    "right": 0.72,
    "far_right": 0.87,
}
ZONE_LABELS = {
    "far_left": "the far left",
    "left": "the left side",
    "center": "the center",
    "right": "the right side",
    "far_right": "the far right",
}
# Two characters never stand closer than this (fraction of the stage width).
MIN_CHARACTER_GAP = 0.2


def nearest_zone(x: float, zones: dict[str, float] | None = None) -> str:
    zones = zones or DEFAULT_ZONES
    generic = {k: v for k, v in zones.items() if k in ZONE_ORDER} or DEFAULT_ZONES
    return min(generic, key=lambda name: abs(generic[name] - x))


def side_of(x: float) -> str:
    """Rough screen side for prompts and dialogue: 'on the left' ..."""
    if x < 0.2:
        return "on the far left"
    if x < 0.42:
        return "on the left"
    if x <= 0.58:
        return "in the center"
    if x <= 0.8:
        return "on the right"
    return "on the far right"


@dataclass(frozen=True)
class ObjectType:
    id: str
    label: str  # how the characters call it: "a frog"
    kind: str  # creature | prop | landmark | effect
    y: float = 0.9  # default stage y of its base (fraction)
    width: float = 0.06
    height: float = 0.07
    magic: str = "none"  # what Mika's magic does to it: vanish | glow | none
    states: tuple[str, ...] = ("idle",)


OBJECT_TYPES: dict[str, ObjectType] = {
    t.id: t
    for t in (
        ObjectType(
            "frog",
            "a frog",
            "creature",
            0.93,
            0.05,
            0.05,
            "vanish",
            ("idle", "croaking", "hopping"),
        ),
        ObjectType(
            "rabbit",
            "a little rabbit",
            "creature",
            0.92,
            0.05,
            0.07,
            "vanish",
            ("idle", "hopping"),
        ),
        ObjectType(
            "butterfly",
            "a glowing butterfly",
            "creature",
            0.55,
            0.04,
            0.04,
            "vanish",
            ("fluttering",),
        ),
        ObjectType(
            "owl",
            "an owl",
            "creature",
            0.3,
            0.06,
            0.08,
            "vanish",
            ("perched", "hooting"),
        ),
        ObjectType(
            "bush",
            "a rustling bush",
            "prop",
            0.95,
            0.12,
            0.12,
            "none",
            ("still", "rustling"),
        ),
        ObjectType("rock", "a mossy rock", "prop", 0.96, 0.1, 0.07, "none", ("still",)),
        ObjectType(
            "boulder",
            "a big boulder blocking the path",
            "prop",
            0.96,
            0.16,
            0.18,
            "none",
            ("blocking", "moved"),
        ),
        ObjectType(
            "campfire",
            "a campfire",
            "prop",
            0.96,
            0.09,
            0.1,
            "none",
            ("burning", "embers", "out"),
        ),
        ObjectType(
            "lantern",
            "a paper lantern",
            "prop",
            0.5,
            0.05,
            0.08,
            "glow",
            ("dim", "glowing"),
        ),
        ObjectType(
            "signpost",
            "an old signpost",
            "landmark",
            0.96,
            0.08,
            0.2,
            "none",
            ("still",),
        ),
        ObjectType(
            "glow_stone",
            "a glowing stone",
            "landmark",
            0.95,
            0.06,
            0.05,
            "glow",
            ("dim", "glowing"),
        ),
        ObjectType(
            "crystal",
            "a star crystal",
            "landmark",
            0.93,
            0.06,
            0.1,
            "glow",
            ("dim", "glowing"),
        ),
        ObjectType(
            "mushrooms",
            "glowing mushrooms",
            "prop",
            0.96,
            0.07,
            0.05,
            "glow",
            ("dim", "glowing"),
        ),
        ObjectType(
            "shrine",
            "a tiny stone shrine",
            "landmark",
            0.95,
            0.09,
            0.14,
            "glow",
            ("dim", "glowing"),
        ),
        ObjectType(
            "fallen_log",
            "a fallen log across the path",
            "prop",
            0.96,
            0.2,
            0.08,
            "none",
            ("blocking", "moved"),
        ),
        ObjectType(
            "rope_bridge",
            "a rope bridge",
            "landmark",
            0.9,
            0.3,
            0.1,
            "none",
            ("steady", "swaying"),
        ),
        ObjectType(
            "map_scroll",
            "an old map scroll",
            "prop",
            0.95,
            0.05,
            0.04,
            "none",
            ("rolled", "open"),
        ),
        # Things Live2D motions draw themselves (Mika's spells); kept so
        # the characters can look at them and know they are there.
        ObjectType(
            "heart",
            "Mika's magic heart",
            "effect",
            0.16,
            0.06,
            0.08,
            "none",
            ("glowing",),
        ),
        ObjectType(
            "game_board", "the game board", "prop", 0.62, 0.44, 0.52, "none", ("up",)
        ),
    )
}


def object_type(type_id: str) -> ObjectType:
    return OBJECT_TYPES.get(type_id) or ObjectType(
        type_id, type_id.replace("_", " "), "prop"
    )
