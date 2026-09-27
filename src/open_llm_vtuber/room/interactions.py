"""World interactions with real consequences.

Mika's magic on a frog is not a cosmetic animation: it is validated against
the world (is there a frog? can magic affect it? can Mika do magic?), then
staged as a short deterministic timeline:

    Mika looks at the frog -> spell motion -> magic bolt effect and sound
    -> (when the bolt lands) the frog vanishes from the screen AND from
    RoomState -> one line of causal history -> Luna reacts.

The state change is scheduled on the room timeline for the moment the effect
lands, so the renderer and the state change together. No LLM decides any of
it; the Conversation Director is only told what happened.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from .world_catalog import object_type

if TYPE_CHECKING:  # pragma: no cover
    from .session import RoomSession

# The spell motion and when its bolt leaves the wand / lands on the target.
SPELL_ACTIONS = ("magic_heart", "summon_rabbit")
BOLT_LEAVES = 1.1
BOLT_LANDS = 1.9


@dataclass
class Outcome:
    performed: bool
    reason: str = ""
    ops: list[dict[str, Any]] = field(default_factory=list)
    description: str = ""  # what the character is doing, for her reply


class Interactions:
    def __init__(self, session: "RoomSession"):
        self.session = session
        self.performed = 0
        self.refused = 0

    def _name(self, character_id: str) -> str:
        profile = self.session.room.get(character_id)
        return profile.name if profile else character_id

    def can(self, character_id: str, ability: str) -> bool:
        profile = self.session.room.get(character_id)
        return bool(profile and ability in profile.abilities)

    def magic_users(self) -> list[str]:
        available = self.session.state.available_characters()
        return [cid for cid in available if self.can(cid, "magic")]

    def cast_on(self, character_id: str, object_id: str, cause: str = "") -> Outcome:
        """Magic on a world object. Returns what happened (or why not)."""
        session = self.session
        if not self.can(character_id, "magic"):
            self.refused += 1
            return Outcome(False, f"{self._name(character_id)} cannot do magic")
        if session.show.engine.playing:
            self.refused += 1
            return Outcome(
                False, "a game is on the board; magic can wait until it ends"
            )
        obj = session.state.objects.get(object_id)
        if not obj or not obj.visible or not obj.type:
            self.refused += 1
            return Outcome(False, "that is not here right now")
        kind = object_type(obj.type)
        if kind.magic == "none" or not obj.interactable:
            self.refused += 1
            return Outcome(False, f"magic does nothing to {obj.label}")
        name = self._name(character_id)
        ops: list[dict[str, Any]] = []
        look = session.attention_op(
            character_id, f"OBJECT:{object_id}", "interaction", 3.5
        )
        if look:
            ops.append(look)
        profile = session.room.get(character_id)
        spell = next(
            (
                a
                for a in SPELL_ACTIONS
                if profile and profile.capabilities and profile.capabilities.get(a)
            ),
            None,
        )
        if spell:
            op = session.action_op(character_id, spell)
            if op:
                ops.append(op)  # not followed: no stray world object from the motion
        state = session.state.characters.get(character_id)
        if state:
            state.activity = f"casting a spell on {obj.label}"
        ops.append(
            {
                "op": "world_fx",
                "name": "magic_bolt",
                "character": character_id,
                "target": object_id,
                "delay_ms": int(BOLT_LEAVES * 1000),
                "ms": int((BOLT_LANDS - BOLT_LEAVES) * 1000),
            }
        )
        ops.append(
            {
                "op": "world_sfx",
                "name": "magic",
                "pan": round(obj.x * 2 - 1, 2),
                "delay_ms": int(BOLT_LEAVES * 1000),
            }
        )
        label = obj.label

        def land() -> list[dict[str, Any]]:
            out: list[dict[str, Any]] = []
            current = session.state.objects.get(object_id)
            if state:
                state.activity = ""
            if not current or not current.visible:
                return out
            if kind.magic == "vanish":
                out += session.world.remove_object(
                    object_id,
                    note=f"{name}'s spell made {label} vanish{cause}",
                    effect="poof",
                )
                out.append(
                    {
                        "op": "world_sfx",
                        "name": "fizzle",
                        "pan": round(current.x * 2 - 1, 2),
                    }
                )
            else:
                out += session.world.set_object_state(
                    object_id,
                    "glowing",
                    note=f"{name}'s spell made {label} glow{cause}",
                )
                out.append({"op": "world_fx", "name": "sparkles", "target": object_id})
            for other in session.state.available_characters():
                if other != character_id:
                    reaction = session.actions.react(
                        other, "surprised", 0.2, force=True
                    )
                    if reaction:
                        out.append(reaction)
                    glance = session.attention_op(
                        other, f"CHARACTER:{character_id}", "interaction", 2.0, 0.6
                    )
                    if glance:
                        out.append(glance)
            return out

        session.timeline.after(BOLT_LANDS, land)
        self.performed += 1
        result = "vanish" if kind.magic == "vanish" else "glow"
        return Outcome(
            True,
            "",
            ops,
            f"casting a spell on {label}; in a moment it will {result}",
        )

    def target_for_magic(self, text: str) -> Optional[str]:
        obj = self.session.world.find(text)
        return obj.id if obj else None
