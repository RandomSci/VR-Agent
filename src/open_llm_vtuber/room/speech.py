"""Per-character voices and the Speaking Coordinator.

* ``CharacterVoices`` builds one TTS engine per character from its yaml voice
  (``inherit`` reuses the conf.yaml engine). Engines are created lazily, so a
  character that never speaks never opens a TTS connection.
* ``SpeakingCoordinator`` speaks one line at a time on the room page: TTS in
  that character's voice, audio tagged with the character id so the right
  model lip syncs, then it waits until the page reports playback complete.

Speech costs TTS requests, so callers must only speak for viewer-triggered
work. ``RoomSession.speech_allowed`` enforces that.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

from loguru import logger

from . import events as ev

if TYPE_CHECKING:  # pragma: no cover
    from .session import RoomSession

TTSFactoryFn = Callable[..., Any]


def _default_factory(engine_type: str, **kwargs: Any) -> Any:
    from ..tts.tts_factory import TTSFactory

    return TTSFactory.get_tts_engine(engine_type, **kwargs)


class CharacterVoices:
    def __init__(
        self,
        session: "RoomSession",
        base_tts_config: Any = None,
        base_engine: Any = None,
        factory: TTSFactoryFn = _default_factory,
    ):
        self.session = session
        self.base_config = base_tts_config
        self.base_engine = base_engine
        self.factory = factory
        self._engines: dict[str, Any] = {}
        self.errors: dict[str, str] = {}
        # Optional callable returning (conf.yaml tts_config, tts_engine) at call time.
        self.base_source = None

    def _base_settings(self, model: str) -> dict[str, Any]:
        config, _ = self._base()
        if not config or getattr(config, "tts_model", None) != model:
            return {}
        try:
            return getattr(config, model.lower()).model_dump()
        except Exception:
            return {}

    def _base(self) -> tuple[Any, Any]:
        """conf.yaml's TTS config and engine, read when needed.

        The server creates the room before it loads conf.yaml, so the engine
        must be looked up at speaking time, not captured at startup.
        """
        source = getattr(self, "base_source", None)
        if source is not None:
            try:
                config, engine = source()
                return config, engine
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(f"VR Room: base TTS lookup failed: {exc}")
        return self.base_config, self.base_engine

    def engine(self, character_id: str) -> Any:
        profile = self.session.room.get(character_id)
        if not profile:
            return None
        if not profile.voice.tts_model:
            # "inherit": always the current conf.yaml engine (follows config switches).
            _, engine = self._base()
            if engine is None:
                if character_id not in self.errors:
                    logger.error(
                        f"VR Room: {character_id} inherits conf.yaml TTS but it is not loaded yet"
                    )
                self.errors[character_id] = "conf.yaml TTS not loaded"
                return None
            self.errors.pop(character_id, None)
            return engine
        if self._engines.get(character_id) is not None:
            return self._engines[character_id]
        engine = None
        try:
            settings = {
                **self._base_settings(profile.voice.tts_model),
                **profile.voice.settings,
            }
            engine = self.factory(profile.voice.tts_model, **settings)
            if engine is not None:
                try:
                    engine._vr_usage_source = f"room:{character_id}"
                except Exception:
                    pass
        except Exception as exc:
            self.errors[character_id] = str(exc)[:200]
            logger.error(f"VR Room: voice for {character_id} unavailable: {exc}")
            engine = None
        if engine is not None:
            # Created once and reused; a failed creation is retried next time.
            self._engines[character_id] = engine
            self.errors.pop(character_id, None)
        return engine

    def describe(self) -> dict[str, Any]:
        return {
            cid: {
                "ready": self.engine(cid) is not None,
                "error": self.errors.get(cid),
            }
            for cid in self.session.state.characters
        }


class SpeakingCoordinator:
    """One voice at a time. Every line goes through ``say``."""

    def __init__(
        self, session: "RoomSession", voices: CharacterVoices, clock=time.time
    ):
        self.session = session
        self.voices = voices
        self.clock = clock
        self.lock = asyncio.Lock()
        self.lines_spoken = 0

    @property
    def busy(self) -> bool:
        return self.lock.locked()

    async def say(
        self, character_id: str, text: str, addressee: Optional[str] = None
    ) -> bool:
        """Speak one line. Returns True when audio was produced and played."""
        from ..agent.output_types import DisplayText
        from ..conversations.tts_manager import TTSTaskManager
        from ..message_handler import message_handler

        profile = self.session.room.get(character_id)
        target = self.session.speech_target()
        text = " ".join(str(text or "").split())[:300]
        if not profile or not target or not text:
            return False
        engine = self.voices.engine(character_id)
        if engine is None:
            self.session.record_failure(character_id, "tts unavailable")
            return False
        client_uid, send = target
        produced = {"audio": 0, "silent": 0}

        async def tagged_send(payload: str) -> None:
            if payload.startswith('{"type": "audio"'):
                data = json.loads(payload)
                data["character"] = character_id
                data["emotion_mode"] = "profile"
                if data.get("audio"):
                    produced["audio"] += 1
                else:
                    produced["silent"] += 1
                payload = json.dumps(data)
            await send(payload)

        async with self.lock:
            character = self.session.state.characters.get(character_id)
            if character:
                character.speaking = True
            self.session.state.current_speaker, self.session.state.previous_speaker = (
                character_id,
                self.session.state.current_speaker,
            )
            seconds = min(20.0, 1.2 + len(text) * 0.065)
            await self.session.push(
                self.session.emit(
                    ev.SPEECH_STARTED,
                    character=character_id,
                    addressee=addressee,
                    seconds=seconds,
                )
            )
            manager = TTSTaskManager()
            ok = False
            try:
                await manager.speak(
                    tts_text=text,
                    display_text=DisplayText(text=text, name=profile.name, avatar=None),
                    actions=None,
                    live2d_model=None,
                    tts_engine=engine,
                    websocket_send=tagged_send,
                )
                if manager.task_list:
                    await asyncio.gather(*manager.task_list)
                # Let the ordered sender flush before announcing completion.
                for _ in range(50):
                    if manager._payload_queue.empty():
                        break
                    await asyncio.sleep(0.01)
                waiter = asyncio.create_task(
                    message_handler.wait_for_response(
                        client_uid, "frontend-playback-complete", timeout=seconds + 12
                    )
                )
                await asyncio.sleep(0)
                await send(json.dumps({"type": "backend-synth-complete"}))
                await waiter
                ok = produced["audio"] > 0
                if ok:
                    self.lines_spoken += 1
                    self.session.record_success(character_id)
                    self.session.state.add_line(character_id, text)
                    if character:
                        character.recent_dialogue.append(text)
                        character.spoken_turns += 1
                else:
                    self.session.record_failure(character_id, "tts produced no audio")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"VR Room: speech for {character_id} failed: {exc}")
                self.session.record_failure(character_id, f"speech: {exc}")
            finally:
                manager.clear()
                if character:
                    character.speaking = False
                self.session.state.current_speaker = None
                await self.session.push(
                    self.session.emit(ev.SPEECH_ENDED, character=character_id)
                )
            return ok
