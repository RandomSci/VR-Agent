"""World Director: the objects that exist in the room.

Permanent objects (the game board) come from room.yaml. Transient objects
appear when certain actions play, for example a rabbit next to Mika while
summon_rabbit runs. Objects are only positions the characters can look at;
the visuals themselves come from the Live2D motion or the game board.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Optional

from . import events as ev
from .state import RoomObject

if TYPE_CHECKING:  # pragma: no cover
    from .session import RoomSession


class WorldDirector:
    def __init__(self, session: "RoomSession", clock=time.time):
        self.session = session
        self.clock = clock
        session.bus.subscribe(ev.ACTION_STARTED, self.on_action_started)

    def object_op(self, obj: RoomObject) -> dict[str, Any]:
        return {"op": "object", **obj.snapshot()}

    def spawn(
        self,
        object_id: str,
        x: float,
        y: float,
        seconds: float,
        source: Optional[str] = None,
        after: float = 0.0,
    ) -> list[dict[str, Any]]:
        obj = RoomObject(
            id=object_id,
            x=max(0.0, min(1.0, x)),
            y=max(0.0, min(1.0, y)),
            width=0.06,
            height=0.08,
            expires_at=self.clock() + after + max(1.0, seconds),
        )
        self.session.state.objects[object_id] = obj
        ops = [self.object_op(obj)]
        ops += self.session.bus.emit(
            ev.OBJECT_APPEARED,
            id=object_id,
            source=source,
            seconds=seconds,
            after=after,
        )
        return ops

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
        return self.spawn(
            rule["object"],
            profile.layout.x + rule["dx"],
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
        return ops
