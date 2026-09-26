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


class FallbackTTS:
    """Speaks with ``primary``; if it raises or returns no file, uses ``fallback``."""

    FAILURES_BEFORE_REST = 3
    REST_SECONDS = 600.0

    def __init__(self, primary: Any, fallback: Any, label: str = "", clock=time.time):
        self.primary = primary
        self.fallback = fallback
        self.label = label
        self.clock = clock
        self.fallbacks_used = 0
        self.failures_in_a_row = 0
        self.rest_until = 0.0
        self.last_error = ""

    def _primary_resting(self) -> bool:
        return self.clock() < self.rest_until

    def _failed(self, reason: str) -> None:
        self.failures_in_a_row += 1
        self.last_error = reason[:200]
        logger.warning(
            f"VR Room: primary voice for {self.label} failed ({reason[:120]}); using the fallback voice"
        )
        if self.failures_in_a_row >= self.FAILURES_BEFORE_REST:
            # A broken voice (bad voice_id, no credits) should not cost a
            # wasted request and extra delay on every single line.
            self.rest_until = self.clock() + self.REST_SECONDS
            self.failures_in_a_row = 0
            logger.error(
                f"VR Room: {self.label}'s primary voice keeps failing; fallback only for "
                f"{int(self.REST_SECONDS / 60)} minutes"
            )

    @staticmethod
    def _ok(path: Any) -> bool:
        if not path:
            return False
        try:
            import os

            return os.path.exists(str(path)) and os.path.getsize(str(path)) > 0
        except Exception:
            return False

    async def async_generate_audio(self, text: str, file_name_no_ext=None) -> Any:
        if not self._primary_resting():
            try:
                path = await self.primary.async_generate_audio(text, file_name_no_ext)
                if self._ok(path):
                    self.failures_in_a_row = 0
                    return path
                self._failed("no audio returned")
            except Exception as exc:
                self._failed(str(exc))
        self.fallbacks_used += 1
        return await self.fallback.async_generate_audio(text, file_name_no_ext)

    def generate_audio(self, text: str, file_name_no_ext=None) -> Any:
        if not self._primary_resting():
            try:
                path = self.primary.generate_audio(text, file_name_no_ext)
                if self._ok(path):
                    self.failures_in_a_row = 0
                    return path
                self._failed("no audio returned")
            except Exception as exc:
                self._failed(str(exc))
        self.fallbacks_used += 1
        return self.fallback.generate_audio(text, file_name_no_ext)

    def remove_file(self, filepath: str, verbose: bool = True) -> None:
        self.primary.remove_file(filepath, verbose)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.primary, name)


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
        # conf.yaml keeps a block per engine (elevenlabs_tts: api_key, model_id, ...),
        # even when another engine is active, so a character override only needs voice_id.
        config, _ = self._base()
        if not config:
            return {}
        block = getattr(config, model.lower(), None)
        if block is None:
            return {}
        try:
            return {k: v for k, v in block.model_dump().items() if v not in (None, "")}
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
            engine = self._build(profile.voice, character_id)
        except Exception as exc:
            self.errors[character_id] = str(exc)[:200]
            logger.error(f"VR Room: voice for {character_id} unavailable: {exc}")
            engine = None
        fallback_spec = getattr(profile.voice, "fallback", None)
        if fallback_spec is not None and fallback_spec.tts_model:
            try:
                backup = self._build(fallback_spec, character_id)
            except Exception as exc:
                logger.error(
                    f"VR Room: fallback voice for {character_id} unavailable: {exc}"
                )
                backup = None
            if engine is None and backup is not None:
                logger.warning(f"VR Room: {character_id} is using the fallback voice")
                engine = backup
            elif engine is not None and backup is not None:
                engine = FallbackTTS(engine, backup, character_id)
                engine._vr_usage_source = f"room:{character_id}"
        if engine is not None:
            # Created once and reused; a failed creation is retried next time.
            self._engines[character_id] = engine
            self.errors.pop(character_id, None)
        return engine

    def _build(self, spec: Any, character_id: str) -> Any:
        base = self._base_settings(spec.tts_model)
        settings = {**base, **spec.settings}
        choices = settings.pop("voice_ids", None)
        if isinstance(choices, list) and choices:
            # First voice that is not the one conf.yaml (the other character) uses.
            taken = str(base.get("voice_id") or base.get("voice") or "")
            pick = next(
                (str(v) for v in choices if str(v) and str(v) != taken), str(choices[0])
            )
            settings["voice_id"] = pick
            if pick != str(choices[0]):
                logger.info(
                    f"VR Room: {character_id} uses voice {pick}; the first choice is already taken"
                )
        engine = self.factory(spec.tts_model, **settings)
        if engine is not None:
            try:
                engine._vr_usage_source = f"room:{character_id}"
            except Exception:
                pass
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
