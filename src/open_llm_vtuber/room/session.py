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
import re
import time
from collections import deque
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from . import events as ev
from .actions import ActionDirector
from .attention import AttentionDirector
from .events import EventBus
from .live_message import LiveMessage
from .profiles import RoomConfig
from .show import ShowRunner
from .speech import CharacterVoices, SpeakingCoordinator
from .state import AttentionTarget, CharacterState, RoomObject, RoomState
from .world import WorldDirector

Send = Callable[[str], Awaitable[None]]

OP_KINDS = (
    "attention",
    "action",
    "speaking",
    "object",
    "camera",
    "sfx",
    "board",
    "line",
    "chat_seen",
)


class RoomSession:
    TICK_SECONDS = 0.25

    def __init__(
        self,
        room: RoomConfig,
        rng: Optional[random.Random] = None,
        clock=time.time,
        registry=None,
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
        from .camera import CameraDirector

        self.camera = CameraDirector(self, clock=clock)
        # Speech (TTS) and games. Speech is only allowed for viewer-triggered work.
        self.voices = CharacterVoices(self)
        self.speech = SpeakingCoordinator(self, self.voices, clock=clock)
        from ..games.engine import EngineSettings

        self.show = ShowRunner(
            self,
            registry=registry,
            settings=EngineSettings(
                pause_after_seconds=room.game_pause_after_seconds,
                end_after_seconds=room.game_end_after_seconds,
            ),
            rng=self.rng,
            clock=clock,
        )
        self._conversation_paused_game = False
        self._last_chat_seen = float("-inf")
        from .director import ConversationDirector

        self.director = ConversationDirector(self, rng=self.rng, clock=clock)
        self.traces: deque[dict[str, Any]] = deque(maxlen=300)

    def trace(self, name: str, **data: Any) -> None:
        """Observability: where time goes in each interaction (developer only)."""
        entry = {"trace": name, "at": round(self.clock(), 3)}
        for key, value in data.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                entry[key] = value if not isinstance(value, str) else value[:120]
            elif isinstance(value, (list, dict)):
                entry[key] = value
        self.traces.append(entry)
        interesting = {
            k: v for k, v in entry.items() if k not in ("trace", "at", "turns")
        }
        logger.info(f"VR Room {name} {interesting}")

    def configure_voices(self, base_tts_config: Any, base_engine: Any) -> None:
        """Called by the server with conf.yaml's TTS so 'inherit' voices work."""
        self.voices.base_config = base_tts_config
        self.voices.base_engine = base_engine

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
        """Local housekeeping and game clock. Never calls an LLM or TTS itself.

        Game lines queued here are only spoken when ``speech_allowed`` says a
        viewer is around; otherwise they are shown as captions.
        """
        ops = self.world.expire()
        ops += self.show.tick()
        return ops

    # ------------------------------------------------------------------
    # viewers, speech gating and failures
    # ------------------------------------------------------------------
    def speech_target(self) -> Optional[tuple[str, Send]]:
        for client_uid, send in self._clients.items():
            models = self._client_models.get(client_uid)
            if models is None or models.get("loaded"):
                return client_uid, send
        return None

    def speech_allowed(self, now: Optional[float] = None) -> bool:
        """TTS may only run for viewer-triggered work: someone chatted recently."""
        from ..vr_agent.state import runtime

        now = self.clock() if now is None else now
        if runtime.paused or not self.state.last_viewer_at or not self.speech_target():
            return False
        return now - self.state.last_viewer_at <= self.room.speech_window_seconds

    def note_viewer_activity(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        self.state.last_viewer_at = now
        self.show.engine.notify_viewer_activity(now)

    def observe_viewer_message(self, message: LiveMessage) -> bool:
        """Every accepted chat message passes here first.

        Game commands and game answers are consumed (they never reach the LLM).
        Returns True when consumed.
        """
        if message.is_system:
            return False
        self.note_viewer_activity()
        self._chat_seen()
        self.trace(
            "viewer_message_received",
            platform=message.platform,
            user=message.display_name,
        )
        camera_ops = self._camera_request(message)
        if camera_ops is not None:
            from ..vr_agent.usage import usage

            usage.record_viewer_interaction()
            self._push_soon(camera_ops)
            return True
        try:
            consumed, ops = self.show.observe(message)
        except Exception as exc:  # a game problem must not break chat
            logger.error(f"VR Room: game observe failed: {exc}")
            return False
        if consumed:
            from ..vr_agent.usage import usage

            usage.record_viewer_interaction()
        if ops:
            try:
                asyncio.get_running_loop().create_task(self.push(ops))
            except RuntimeError:
                pass
        return consumed

    CHAT_SEEN_INTERVAL = 2.0

    def _chat_seen(self) -> None:
        """Tell the room a comment arrived so a character glances at chat at once
        (the reply comes later). Only on viewer messages, so idle stays silent."""
        now = self.clock()
        if now - self._last_chat_seen < self.CHAT_SEEN_INTERVAL:
            return
        self._last_chat_seen = now
        self._push_soon([{"op": "chat_seen"}])

    def _camera_request(self, message: LiveMessage) -> Optional[list[dict[str, Any]]]:
        """'zoom in on Luna', 'close up', 'zoom out'. Consumed, no LLM."""
        from .camera import camera_request

        kind = camera_request(message.clean_text)
        if not kind:
            return None
        target = self.addressed_or_primary(message.clean_text)
        ops = self.camera.request(kind, target)
        if kind == "in" and target and ops:
            ops += self.emit(ev.REACTION, character=target, reaction="shy")
        self.trace("camera_request", kind=kind, character=target, moved=bool(ops))
        return ops

    def addressed_or_primary(self, text: str) -> Optional[str]:
        lowered = f" {str(text).lower()} "
        available = self.state.available_characters()
        for profile in self.room.characters:
            if profile.id not in available:
                continue
            for name in profile.names:
                if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", lowered):
                    return profile.id
        primary = self.room.primary
        if primary and primary.id in available:
            return primary.id
        return available[0] if available else None

    def record_failure(self, character_id: str, reason: str) -> None:
        character = self.state.characters.get(character_id)
        if not character:
            return
        character.failures += 1
        logger.warning(
            f"VR Room: {character_id} failure {character.failures}: {reason}"
        )
        if character.failures >= self.room.director.failure_threshold:
            character.cooldown_until = (
                self.clock() + self.room.director.cooldown_seconds
            )
            character.failures = 0
            logger.error(
                f"VR Room: {character_id} cooling down for {self.room.director.cooldown_seconds:.0f}s"
            )

    def record_success(self, character_id: str) -> None:
        character = self.state.characters.get(character_id)
        if character:
            character.failures = 0

    # ------------------------------------------------------------------
    # conversation slots (replies to normal chat)
    # ------------------------------------------------------------------
    def conversation_slot_available(self) -> bool:
        """A reply may start: nobody is speaking and the game is at a checkpoint."""
        return (
            not self.speech.busy and not self.show.lines and self.show.at_checkpoint()
        )

    def begin_conversation(self) -> None:
        if self.show.engine.playing and not self.show.engine.active.paused:
            self._push_soon(self.show.apply(self.show.engine.pause("conversation")))
            self._conversation_paused_game = True

    def end_conversation(self) -> None:
        if self._conversation_paused_game:
            self._conversation_paused_game = False
            self._push_soon(self.show.apply(self.show.engine.resume()))

    def _push_soon(self, ops: list[dict[str, Any]]) -> None:
        if not ops:
            return
        try:
            asyncio.get_running_loop().create_task(self.push(ops))
        except RuntimeError:
            pass

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
            # A page that reconnects mid game redraws the board from this.
            "board": self.show.engine.view(),
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
        for op in ops:
            if op["op"] == "camera":
                self.trace(
                    "camera_transition_started",
                    shot=op.get("shot"),
                    character=op.get("target"),
                )
            elif op["op"] == "action":
                self.trace(
                    "character_action_started",
                    character=op.get("character"),
                    action=op.get("name"),
                )
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
            "speech_allowed": self.speech_allowed(),
            "voices": self.voices.describe(),
            "show": self.show.status(),
            "director": self.director.status(),
            "camera": self.camera.status(),
            "traces": list(self.traces)[-60:],
            "room": self.room.describe(),
            "clients": {uid: self._client_models.get(uid, {}) for uid in self._clients},
            "state": self.state.snapshot(),
            "events": self.bus.recent(40),
        }
