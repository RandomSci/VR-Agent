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
import contextlib
import contextvars
import json
import os
import re
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
        fallback_only = False
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
                fallback_only = True
            elif engine is not None and backup is not None:
                engine = FallbackTTS(engine, backup, character_id)
                engine._vr_usage_source = f"room:{character_id}"
        if engine is not None:
            # Created once and reused; a failed creation is retried next time.
            self._engines[character_id] = engine
            if not fallback_only:
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
        if (
            spec.tts_model == "elevenlabs_tts"
            and not str(settings.get("api_key") or "").strip()
        ):
            # Without a key every line would fail first and then fall back,
            # which adds a delay before each sentence. Use the fallback directly.
            raise ValueError(
                "elevenlabs_tts has no api_key: fill api_key in conf.yaml under "
                "character_config.tts_config.elevenlabs_tts"
            )
        engine = self.factory(spec.tts_model, **settings)
        if engine is not None:
            try:
                engine._vr_usage_source = f"room:{character_id}"
            except Exception:
                pass
        return engine

    @staticmethod
    def _engine_facts(engine: Any) -> dict[str, Any]:
        """What an engine really speaks with. Never includes keys."""
        if engine is None:
            return {"engine": None}
        inner = engine.primary if isinstance(engine, FallbackTTS) else engine
        module = type(inner).__module__.rsplit(".", 1)[-1]
        facts: dict[str, Any] = {"engine": module}
        for attr in (
            "voice",
            "voice_id",
            "model_id",
            "output_format",
            "stability",
            "similarity_boost",
            "style",
            "use_speaker_boost",
        ):
            value = getattr(inner, attr, None)
            if value is not None and not callable(value):
                facts[attr] = value
        if isinstance(engine, FallbackTTS):
            facts["fallback"] = CharacterVoices._engine_facts(engine.fallback)
            facts["fallback_lines"] = engine.fallbacks_used
            facts["primary_resting"] = engine._primary_resting()
            if engine.last_error:
                facts["last_error"] = engine.last_error
        return facts

    def report(self) -> dict[str, Any]:
        """Per character: which engine, voice and settings actually speak."""
        out: dict[str, Any] = {}
        for cid in self.session.state.characters:
            profile = self.session.room.get(cid)
            engine = self.engine(cid)
            facts = self._engine_facts(engine)
            facts["source"] = (
                "conf.yaml"
                if profile and not profile.voice.tts_model
                else "character yaml"
            )
            if self.errors.get(cid):
                facts["error"] = self.errors[cid]
            out[cid] = facts
        return out

    def log_report(self) -> None:
        for cid, facts in self.report().items():
            main = {k: v for k, v in facts.items() if k not in ("fallback",)}
            logger.info(f"VR Room voice for {cid}: {main}")

    def describe(self) -> dict[str, Any]:
        report = self.report()
        return {
            cid: {
                "ready": report[cid].get("engine") is not None,
                "error": self.errors.get(cid),
                **report[cid],
            }
            for cid in self.session.state.characters
        }


# How a line feels, from its words: edge-tts has no acting styles, but speed
# and pitch per line already make "Argh!!!" sound different from "Nooo...".
# (rate %, pitch Hz) added to the voice's own tuning. VR_EMOTIONAL_VOICE=0: off.
PROSODY = (
    ("frustrated", re.compile(r"\b(argh+|ugh+|hmph|grr+|come on|seriously)\b", re.I), 10, -6),
    ("sad", re.compile(r"\b(no{3,}|oh no|sorry|so close|nooo|aww+)\b", re.I), -10, -14),
    ("surprised", re.compile(r"\b(wait,? what|whoa|woah|what\?!|no way|omg|oh my)\b", re.I), 8, 28),
    ("excited", re.compile(r"\b(yes{2,}|woo+(hoo+)?|wheee+|yay+|nailed it|we did it|amazing)\b|!{2,}", re.I), 12, 22),
)


def _shift(value: str, delta: int, unit: str) -> str:
    match = re.match(r"^\s*([+-]?\d+)", str(value or "0"))
    number = int(match.group(1)) if match else 0
    return f"{number + delta:+d}{unit}"


def emotion_prosody(text: str) -> Optional[tuple[str, int, int]]:
    """(mood, rate %, pitch Hz) for a line, or None when it is calm."""
    if os.environ.get("VR_EMOTIONAL_VOICE", "1").strip().lower() in ("0", "false", "no", "off"):
        return None
    for mood, pattern, rate, pitch in PROSODY:
        if pattern.search(text):
            return mood, rate, pitch
    return None


@contextlib.contextmanager
def emotional_voice(engine: Any, text: str):
    """The voice for this one line (edge-tts style engines with rate and pitch)."""
    feel = emotion_prosody(text)
    if feel is None or not hasattr(engine, "rate") or not hasattr(engine, "pitch"):
        yield
        return
    old = (engine.rate, engine.pitch)
    engine.rate = _shift(engine.rate, feel[1], "%")
    engine.pitch = _shift(engine.pitch, feel[2], "Hz")
    try:
        yield
    finally:
        engine.rate, engine.pitch = old


def playback_wait(audio_ms: float, estimate: float) -> float:
    """How long to wait for the page to say the clip finished playing.

    The page does not always answer (a hidden tab, a second client). Waiting
    the estimate plus 12 s then held every next line for 20 s or more, so
    the wait is the real clip length plus a small margin.
    """
    if audio_ms > 0:
        return audio_ms / 1000.0 + 2.5
    return estimate + 4.0


# True inside a viewer interaction (set by the director for its own task and
# every task it starts). The live handler holds ``lock`` for the whole
# interaction, so a line spoken from inside it (the build acknowledgment, a
# bug reaction) must not wait for that same lock: it would wait forever.
INSIDE_INTERACTION: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "vr_inside_interaction", default=False
)


class SpeakingCoordinator:
    """One voice at a time. Every line goes through ``say``."""

    def __init__(
        self, session: "RoomSession", voices: CharacterVoices, clock=time.time
    ):
        self.session = session
        self.voices = voices
        self.clock = clock
        self.lock = asyncio.Lock()  # held by a whole viewer interaction
        self.voice_lock = asyncio.Lock()  # one line at a time, always
        self.lines_spoken = 0

    @contextlib.asynccontextmanager
    async def _turn(self):
        if INSIDE_INTERACTION.get():
            async with self.voice_lock:
                yield
        else:
            async with self.lock:
                async with self.voice_lock:
                    yield

    @property
    def busy(self) -> bool:
        return self.lock.locked()

    @property
    def talking(self) -> bool:
        """A line is being spoken right now (inside or outside an interaction)."""
        return self.voice_lock.locked()

    async def say(
        self, character_id: str, text: str, addressee: Optional[str] = None
    ) -> bool:
        """Speak one line. Returns True when audio was produced and played."""
        from ..agent.output_types import DisplayText
        from ..conversations.tts_manager import TTSTaskManager
        from ..message_handler import message_handler

        profile = self.session.room.get(character_id)
        target = self.session.speech_target()
        from ..vr_agent.text_safety import strip_emoji

        # Never read an emoji out loud ("smiling face with hearts").
        text = strip_emoji(" ".join(str(text or "").split()))[:300]
        if not profile or not target or not text:
            return False
        engine = self.voices.engine(character_id)
        if engine is None:
            self.session.record_failure(character_id, "tts unavailable")
            return False
        client_uid, send = target
        produced = {"audio": 0, "silent": 0, "ms": 0.0}

        async def tagged_send(payload: str) -> None:
            if payload.startswith('{"type": "audio"'):
                data = json.loads(payload)
                data["character"] = character_id
                data["emotion_mode"] = "profile"
                if data.get("audio"):
                    produced["audio"] += 1
                    # The real length of the clip: volumes are one per slice.
                    produced["ms"] += len(data.get("volumes") or []) * float(
                        data.get("slice_length") or 20
                    )
                else:
                    produced["silent"] += 1
                payload = json.dumps(data)
            await send(payload)

        async with self._turn():
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
            feel = contextlib.ExitStack()
            feel.enter_context(emotional_voice(engine, text))  # one line at a time: the turn lock holds
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
                        client_uid,
                        "frontend-playback-complete",
                        timeout=playback_wait(produced["ms"], seconds),
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
                feel.close()
                manager.clear()
                if character:
                    character.speaking = False
                self.session.state.current_speaker = None
                await self.session.push(
                    self.session.emit(ev.SPEECH_ENDED, character=character_id)
                )
            return ok
