"""World Director: the objects that exist in the room, and what happens to them.

Every object lives in ``RoomState.objects``. The renderer draws typed objects
(frog, campfire, signpost ...) from the ``object`` ops sent here, the
characters can look at them, and ``room/awareness.py`` tells the
characters which ones are visible right now. Appearing, changing state and
disappearing all go through this director, so the screen, the state and
the characters' knowledge never disagree, and each change leaves one line
in the bounded causal history.

Permanent anchors (the game board) come from room.yaml. Some Live2D
motions draw things themselves (Mika's rabbit and heart); those get a
matching object while the motion shows them, so the characters know too.
"""

from __future__ import annotations

import itertools
import re
import time
from typing import TYPE_CHECKING, Any, Optional

from . import events as ev
from .state import RoomObject
from .world_catalog import DEFAULT_ZONES, OBJECT_TYPES, ZONE_LABELS, object_type

if TYPE_CHECKING:  # pragma: no cover
    from .session import RoomSession

_ids = itertools.count(1)
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


class WorldDirector:
    def __init__(self, session: "RoomSession", clock=time.time):
        self.session = session
        self.clock = clock
        self.spawned = 0
        self.removed = 0
        session.bus.subscribe(ev.ACTION_STARTED, self.on_action_started)

    # ------------------------------------------------------------------
    # zones
    # ------------------------------------------------------------------
    def zones(self) -> dict[str, float]:
        scene = self.session.state.scene
        return {**DEFAULT_ZONES, **scene.zones}

    def zone_x(self, zone: str) -> Optional[float]:
        return self.zones().get(zone)

    def zone_label(self, zone: str) -> str:
        scene = self.session.state.scene
        return (
            scene.zone_labels.get(zone)
            or ZONE_LABELS.get(zone)
            or zone.replace("_", " ")
        )

    # ------------------------------------------------------------------
    # scenes
    # ------------------------------------------------------------------
    def scene_op(self, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        op = {"op": "scene", "scene": self.session.state.scene.snapshot()}
        if extra:
            op["scene"].update(extra)
        return op

    def set_scene(
        self, scene, note: str = "", transition: str = ""
    ) -> list[dict[str, Any]]:
        """Move the world to another scene. Objects of the old scene are gone
        (from the screen and the state), so nothing stale lingers."""
        state = self.session.state
        ops = self.clear_scene_objects(keep_scene=scene.id)
        state.scene = scene
        # Characters keep their spots, clamped to the new scene's stage.
        if note:
            state.record("arrived", note)
        ops.append(self.scene_op({"transition": transition} if transition else None))
        return ops

    # ------------------------------------------------------------------
    # objects
    # ------------------------------------------------------------------
    def object_op(self, obj: RoomObject) -> dict[str, Any]:
        return {"op": "object", **obj.snapshot()}

    def spawn_object(
        self,
        type_id: str,
        zone: str = "",
        x: Optional[float] = None,
        object_id: Optional[str] = None,
        state: str = "",
        seconds: float = 0.0,
        source: str = "",
        note: str = "",
        y: Optional[float] = None,
        interactable: Optional[bool] = None,
        label: str = "",
    ) -> list[dict[str, Any]]:
        """Make a typed object exist (and be drawn) in the world."""
        kind = object_type(type_id)
        if x is None:
            x = self.zone_x(zone) if zone else None
        if x is None:
            return []
        state_obj = self.session.state
        object_id = object_id or f"{type_id}_{next(_ids)}"
        if not _ID_RE.match(object_id):
            return []
        if (
            object_id not in state_obj.objects
            and len(state_obj.objects) >= state_obj.MAX_OBJECTS
        ):
            self._evict_oldest()
        from .world_catalog import nearest_zone

        obj = RoomObject(
            id=object_id,
            x=max(0.02, min(0.98, float(x))),
            y=max(0.05, min(1.0, float(y if y is not None else kind.y))),
            width=kind.width,
            height=kind.height,
            expires_at=self.clock() + seconds if seconds > 0 else 0.0,
            type=type_id,
            label=label or kind.label,
            zone=zone or nearest_zone(float(x), self.zones()),
            state=state or kind.states[0],
            interactable=kind.magic != "none" if interactable is None else interactable,
            scene=state_obj.scene.id,
            source=source,
        )
        state_obj.objects[object_id] = obj
        self.spawned += 1
        if note:
            state_obj.record("appeared", note, object_id)
        ops = [self.object_op(obj)]
        ops += self.session.bus.emit(
            ev.OBJECT_APPEARED,
            id=object_id,
            source=source or None,
            seconds=seconds,
            after=0.0,
        )
        return ops

    def set_object_state(
        self, object_id: str, new_state: str, note: str = ""
    ) -> list[dict[str, Any]]:
        obj = self.session.state.objects.get(object_id)
        if not obj or not obj.visible:
            return []
        obj.state = new_state
        if note:
            self.session.state.record("changed", note, object_id)
        return [self.object_op(obj)]

    def remove_object(
        self, object_id: str, note: str = "", effect: str = ""
    ) -> list[dict[str, Any]]:
        """The object is gone: from the screen and from the state."""
        obj = self.session.state.objects.pop(object_id, None)
        if not obj:
            return []
        self.removed += 1
        if note:
            self.session.state.record("vanished", note, object_id)
        op: dict[str, Any] = {"op": "object", "id": object_id, "remove": True}
        if effect:
            op["effect"] = effect
        return [op] + self.session.bus.emit(ev.OBJECT_REMOVED, id=object_id)

    def clear_scene_objects(self, keep_scene: str = "") -> list[dict[str, Any]]:
        """Scene changed: objects of the old scene are gone (quietly)."""
        ops: list[dict[str, Any]] = []
        for object_id, obj in list(self.session.state.objects.items()):
            if obj.type and obj.type != "game_board" and obj.scene != keep_scene:
                ops += self.remove_object(object_id)
        return ops

    def _evict_oldest(self) -> None:
        for object_id, obj in list(self.session.state.objects.items()):
            if obj.type and obj.type != "game_board":
                self.session.state.objects.pop(object_id, None)
                return

    def find(self, text: str) -> Optional[RoomObject]:
        """A visible object a viewer or a line refers to ('the frog')."""
        words = set(re.findall(r"[a-z]+", str(text).lower()))
        best = None
        for obj in self.session.state.visible_objects():
            names = {obj.type, obj.type.replace("_", " ")} | set(
                obj.label.lower().split()
            )
            names |= {w.rstrip("s") for w in names}
            names -= {"a", "an", "the", "little", "old", "big", "of", "across", "path"}
            if words & names or obj.type.replace("_", " ") in str(text).lower():
                if best is None or obj.type in words:
                    best = obj
        return best

    # ------------------------------------------------------------------
    # objects drawn by Live2D motions (Mika's rabbit and heart)
    # ------------------------------------------------------------------
    def spawn(
        self,
        object_id: str,
        x: float,
        y: float,
        seconds: float,
        source: Optional[str] = None,
        after: float = 0.0,
    ) -> list[dict[str, Any]]:
        type_id = object_id if object_id in OBJECT_TYPES else ""
        kind = object_type(object_id)
        obj = RoomObject(
            id=object_id,
            x=max(0.0, min(1.0, x)),
            y=max(0.0, min(1.0, y)),
            width=0.06,
            height=0.08,
            expires_at=self.clock() + after + max(1.0, seconds),
            type=type_id,
            label=kind.label if type_id else "",
            source=source or "",
            by_motion=True,  # the Live2D motion draws it; the renderer does not
        )
        self.session.state.objects[object_id] = obj
        name = self._name(source)
        if type_id and name:
            self.session.state.record(
                "appeared", f"{name} made {kind.label} appear with magic", object_id
            )
        ops = [self.object_op(obj)]
        ops += self.session.bus.emit(
            ev.OBJECT_APPEARED,
            id=object_id,
            source=source,
            seconds=seconds,
            after=after,
        )
        return ops

    def _name(self, character_id: Optional[str]) -> str:
        profile = self.session.room.get(character_id) if character_id else None
        return profile.name if profile else ""

    def on_action_started(self, event: ev.Event):
        character_id = event.get("character")
        action = event.get("action")
        rule = self.session.room.action_objects.get(action)
        profile = (
            self.session.room.get(character_id)
            if isinstance(character_id, str)
            else None
        )
        if (
            not rule
            or not profile
            or not profile.capabilities
            or not profile.capabilities.get(action)
        ):
            return []
        state = self.session.state.characters.get(character_id)
        base_x = state.x if state else profile.layout.x
        return self.spawn(
            rule["object"],
            base_x + rule["dx"],
            rule["y"],
            rule["seconds"],
            source=character_id,
            after=rule.get("after", 0.0),
        )

    def expire(self) -> list[dict[str, Any]]:
        now = self.clock()
        ops = []
        for object_id, obj in list(self.session.state.objects.items()):
            if obj.expires_at and now >= obj.expires_at:
                del self.session.state.objects[object_id]
                ops.append({"op": "object", "id": object_id, "remove": True})
                ops += self.session.bus.emit(ev.OBJECT_REMOVED, id=object_id)
                if (
                    obj.type
                    and obj.type in OBJECT_TYPES
                    and object_type(obj.type).kind == "creature"
                ):
                    self.session.state.record(
                        "left", f"{obj.label} wandered off", object_id
                    )
        return ops

    def describe(self) -> dict[str, Any]:
        return {
            "objects": len(self.session.state.objects),
            "visible": [o.id for o in self.session.state.visible_objects()],
            "spawned": self.spawned,
            "removed": self.removed,
        }
