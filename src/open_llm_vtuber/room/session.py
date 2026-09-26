"""RoomSession: glue between the room state, the directors and room pages.

Room pages are browser clients that opened ``/vr-agent/room.html`` and said
hello with ``mode: room``. The session sends them the cast and a snapshot,
then pushes small typed updates (``vr-room-update`` ops). Every op is built
here from validated data; nothing a viewer typed is forwarded as an op.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from . import events as ev
from .actions import ActionDirector
from .attention import AttentionDirector
from .events import EventBus
from .profiles import RoomConfig
from .state import AttentionTarget, CharacterState, RoomObject, RoomState
from .world import WorldDirector

Send = Callable[[str], Awaitable[None]]

OP_KINDS = ("attention", "action", "speaking", "object", "camera", "sfx", "board")


class RoomSession:
    TICK_SECONDS = 1.0

    def __init__(
        self, room: RoomConfig, rng: Optional[random.Random] = None, clock=time.time
    ):
        self.room = room
        self.clock = clock
        self.rng = rng or random.Random()
        self.state = RoomState()
        for profile in room.characters:
            self.state.characters[profile.id] = CharacterState(
                id=profile.id, name=profile.name
            )
        for object_id, box in room.objects.items():
            self.state.objects[object_id] = RoomObject(id=object_id, **box)
        self._clients: dict[str, Send] = {}
        self._client_models: dict[str, dict[str, list[str]]] = {}
        self._loop_task: Optional[asyncio.Task] = None
        # Directors. All deterministic and local: no LLM, no TTS.
        self.bus = EventBus()
        self.world = WorldDirector(self, clock=clock)
        self.attention = AttentionDirector(self, rng=self.rng)
        self.actions = ActionDirector(self, rng=self.rng, clock=clock)

    # ------------------------------------------------------------------
    # clients
    # ------------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.room.active

    def has_clients(self) -> bool:
        return bool(self._clients)

    def client_uids(self) -> list[str]:
        return list(self._clients)

    async def register(self, client_uid: str, send: Send) -> None:
        self._clients[client_uid] = send
        await self._send(client_uid, self.config_payload())
        self.ensure_loop()
        logger.info(f"VR Room: client {client_uid} registered as a room page.")

    # ------------------------------------------------------------------
    # events and the local tick
    # ------------------------------------------------------------------
    def emit(self, name: str, **data: Any) -> list[dict[str, Any]]:
        """Emit an event and collect renderer ops, including follow-up events.

        Every action op produces an ACTION_STARTED event (the World Director
        may spawn an object, which turns heads). Depth is bounded.
        """
        ops = self.bus.emit(name, **data)
        return self._follow_actions(ops, depth=0)

    def _follow_actions(
        self, ops: list[dict[str, Any]], depth: int
    ) -> list[dict[str, Any]]:
        if depth >= 3:
            return ops
        extra: list[dict[str, Any]] = []
        for op in list(ops):
            if op.get("op") == "action" and not op.get("_followed"):
                op["_followed"] = True
                extra += self.bus.emit(
                    ev.ACTION_STARTED, character=op["character"], action=op["name"]
                )
        if extra:
            ops = ops + self._follow_actions(extra, depth + 1)
        return ops

    def play(
        self, character_id: str, action: str, delay_seconds: float = 0.0
    ) -> list[dict[str, Any]]:
        op = self.action_op(character_id, action, delay_seconds=delay_seconds)
        return self._follow_actions([op], depth=0) if op else []

    async def emit_and_push(self, name: str, **data: Any) -> list[dict[str, Any]]:
        ops = self.emit(name, **data)
        await self.push(ops)
        return ops

    def tick(self) -> list[dict[str, Any]]:
        """Local housekeeping, once a second. Never calls an LLM or TTS."""
        return self.world.expire()

    def ensure_loop(self) -> None:
        if self._loop_task and not self._loop_task.done():
            return
        try:
            self._loop_task = asyncio.get_running_loop().create_task(
                self._run(), name="vr-room-tick"
            )
        except RuntimeError:
            self._loop_task = None

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.TICK_SECONDS)
                ops = self.tick()
                if ops:
                    await self.push(ops)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.error(f"VR Room: tick failed: {exc}")

    def unregister(self, client_uid: str) -> None:
        self._clients.pop(client_uid, None)
        self._client_models.pop(client_uid, None)
        self._recompute_availability()

    def on_client_status(self, client_uid: str, loaded: Any, failed: Any) -> None:
        if client_uid not in self._clients:
            return
        known = set(self.state.characters)
        loaded_ids = [str(x) for x in (loaded or []) if str(x) in known]
        failed_ids = [str(x) for x in (failed or []) if str(x) in known]
        self._client_models[client_uid] = {"loaded": loaded_ids, "failed": failed_ids}
        if failed_ids:
            logger.warning(f"VR Room: client {client_uid} could not load {failed_ids}")
        self._recompute_availability()

    def _recompute_availability(self) -> None:
        if not self._client_models:
            for character in self.state.characters.values():
                character.available = True
            return
        loaded_anywhere = {
            cid for s in self._client_models.values() for cid in s["loaded"]
        }
        for character in self.state.characters.values():
            character.available = character.id in loaded_anywhere

    def config_payload(self) -> dict[str, Any]:
        return {
            "type": "vr-room-config",
            "room": self.room.to_frontend(),
            "snapshot": self.state.snapshot(),
        }

    async def _send(self, client_uid: str, payload: dict[str, Any]) -> None:
        send = self._clients.get(client_uid)
        if not send:
            return
        try:
            await send(json.dumps(payload))
        except Exception as exc:  # pragma: no cover - network dependent
            logger.debug(f"VR Room: send to {client_uid} failed: {exc}")

    async def broadcast(self, payload: dict[str, Any]) -> None:
        for client_uid in list(self._clients):
            await self._send(client_uid, payload)

    async def push(self, ops: list[dict[str, Any]]) -> None:
        ops = [
            {k: v for k, v in op.items() if not k.startswith("_")}
            for op in ops
            if op and op.get("op") in OP_KINDS
        ]
        if ops:
            await self.broadcast(
                {"type": "vr-room-update", "ops": ops, "at": time.time()}
            )

    # ------------------------------------------------------------------
    # typed ops
    # ------------------------------------------------------------------
    def attention_op(
        self,
        character_id: str,
        target: AttentionTarget | str,
        source: str = "director",
        hold_seconds: float = 4.0,
        delay_seconds: float = 0.0,
    ) -> Optional[dict[str, Any]]:
        character = self.state.characters.get(character_id)
        if not character:
            return None
        if not isinstance(target, AttentionTarget):
            target = AttentionTarget.parse(target)
        if target.kind == "CHARACTER" and (
            target.ref == character_id or target.ref not in self.state.characters
        ):
            return None
        if target.kind == "OBJECT" and target.ref not in self.state.objects:
            return None
        hold_seconds = max(0.5, min(60.0, float(hold_seconds)))
        delay_seconds = max(0.0, min(10.0, float(delay_seconds)))
        character.attention = target
        character.attention_source = source
        character.attention_until = self.clock() + delay_seconds + hold_seconds
        return {
            "op": "attention",
            "character": character_id,
            "target": str(target),
            "hold_ms": int(hold_seconds * 1000),
            "delay_ms": int(delay_seconds * 1000),
        }

    def action_op(
        self,
        character_id: str,
        action: str,
        delay_seconds: float = 0.0,
        sync: str = "now",
    ) -> Optional[dict[str, Any]]:
        profile = self.room.get(character_id)
        if (
            not profile
            or not profile.capabilities
            or not profile.capabilities.get(action)
        ):
            return None
        character = self.state.characters[character_id]
        character.action = action
        return {
            "op": "action",
            "character": character_id,
            "name": action,
            "delay_ms": int(max(0.0, min(10.0, delay_seconds)) * 1000),
            "sync": "speech" if sync == "speech" else "now",
        }

    def status(self) -> dict[str, Any]:
        from ..vr_agent.usage import usage

        return {
            "usage": usage.snapshot(),
            "room": self.room.describe(),
            "clients": {uid: self._client_models.get(uid, {}) for uid in self._clients},
            "state": self.state.snapshot(),
            "events": self.bus.recent(40),
        }
