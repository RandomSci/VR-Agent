from typing import Dict, List, Optional, Callable, TypedDict
from fastapi import WebSocket, WebSocketDisconnect
import asyncio
import json
import os
import time
from enum import Enum
import numpy as np
from loguru import logger

from .service_context import ServiceContext
from .chat_group import (
    ChatGroupManager,
    handle_group_operation,
    handle_client_disconnect,
    broadcast_to_group,
)
from .message_handler import message_handler
from .utils.stream_audio import prepare_audio_payload
from .chat_history_manager import (
    create_new_history,
    get_history,
    delete_history,
    get_history_list,
)
from .config_manager.utils import scan_config_alts_directory, scan_bg_directory
from .conversations.conversation_handler import (
    handle_conversation_trigger,
    handle_group_interrupt,
    handle_individual_interrupt,
)
from .conversations.single_conversation import process_single_conversation
from .live.youtube_live import YouTubeChatMessage
from .vr_agent import VRAgentState, load_capabilities, resolve_intent, runtime
from .vr_agent.capabilities import CharacterCapabilities
from .vr_agent.intent import NO_INTENT
from .vr_agent.prompting import build_livestream_prompt
from .vr_agent.text_safety import clean_viewer_text
from .vr_agent.usage import usage
from .room.profiles import RoomConfig, load_room
from .room.session import RoomSession


class MessageType(Enum):
    """Enum for WebSocket message types"""

    GROUP = ["add-client-to-group", "remove-client-from-group"]
    HISTORY = [
        "fetch-history-list",
        "fetch-and-set-history",
        "create-new-history",
        "delete-history",
    ]
    CONVERSATION = ["mic-audio-end", "text-input", "ai-speak-signal"]
    CONFIG = ["fetch-configs", "switch-config"]
    CONTROL = ["interrupt-signal", "audio-play-start"]
    DATA = ["mic-audio-data"]


class WSMessage(TypedDict, total=False):
    """Type definition for WebSocket messages"""

    type: str
    action: Optional[str]
    text: Optional[str]
    audio: Optional[List[float]]
    images: Optional[List[str]]
    history_uid: Optional[str]
    file: Optional[str]
    display_text: Optional[dict]


ROOM_INTERACTION_KEY = "vr-room-interaction"


class WebSocketHandler:
    """Handles WebSocket connections and message routing"""

    def __init__(self, default_context_cache: ServiceContext):
        """Initialize the WebSocket handler with default context"""
        self.client_connections: Dict[str, WebSocket] = {}
        self.client_contexts: Dict[str, ServiceContext] = {}
        self.chat_group_manager = ChatGroupManager()
        self.current_conversation_tasks: Dict[str, Optional[asyncio.Task]] = {}
        self.default_context_cache = default_context_cache
        self.received_data_buffers: Dict[str, np.ndarray] = {}
        # Clients that announced themselves as the livestream (OBS) page.
        self.live_client_uids: set[str] = set()
        self._capabilities_cache: Dict[str, CharacterCapabilities] = {}
        runtime.set_broadcaster(self.broadcast_to_all)
        # VR Room (multi-character page). Any problem here leaves the classic
        # single-character livestream untouched.
        self.room_client_uids: set[str] = set()
        try:
            self.room_session = RoomSession(load_room())
        except Exception as exc:
            logger.error(f"VR Room disabled: {exc}")
            self.room_session = RoomSession(RoomConfig())
        # conf.yaml's TTS is loaded after this handler exists, so voices that
        # inherit it look it up when they speak, not now.
        def _base_voice(ctx=default_context_cache):
            character = getattr(ctx, "character_config", None)
            return (
                getattr(character, "tts_config", None) if character else None,
                getattr(ctx, "tts_engine", None),
            )

        self.room_session.voices.base_source = _base_voice
        from .room.question_maker import conf_llm_source

        # AI trivia questions use conf.yaml's LLM, only when a viewer starts Trivia.
        self.room_session.show.question_maker.llm_source = conf_llm_source(
            lambda: self.default_context_cache
        )

        # Message handlers mapping
        self._message_handlers = self._init_message_handlers()

    def _init_message_handlers(self) -> Dict[str, Callable]:
        """Initialize message type to handler mapping"""
        return {
            "add-client-to-group": self._handle_group_operation,
            "remove-client-from-group": self._handle_group_operation,
            "request-group-info": self._handle_group_info,
            "fetch-history-list": self._handle_history_list_request,
            "fetch-and-set-history": self._handle_fetch_history,
            "create-new-history": self._handle_create_history,
            "delete-history": self._handle_delete_history,
            "interrupt-signal": self._handle_interrupt,
            "mic-audio-data": self._handle_audio_data,
            "mic-audio-end": self._handle_conversation_trigger,
            "raw-audio-data": self._handle_raw_audio_data,
            "text-input": self._handle_conversation_trigger,
            "ai-speak-signal": self._handle_conversation_trigger,
            "fetch-configs": self._handle_fetch_configs,
            "switch-config": self._handle_config_switch,
            "fetch-backgrounds": self._handle_fetch_backgrounds,
            "audio-play-start": self._handle_audio_play_start,
            "vr-room-audio-diagnostic": self._handle_vr_room_audio_diagnostic,
            "request-init-config": self._handle_init_config_request,
            "heartbeat": self._handle_heartbeat,
            "vr-agent-hello": self._handle_vr_agent_hello,
            "vr-room-client-status": self._handle_vr_room_client_status,
        }

    async def handle_new_connection(
        self, websocket: WebSocket, client_uid: str
    ) -> None:
        """
        Handle new WebSocket connection setup

        Args:
            websocket: The WebSocket connection
            client_uid: Unique identifier for the client

        Raises:
            Exception: If initialization fails
        """
        try:
            session_service_context = await self._init_service_context(
                websocket.send_text, client_uid
            )

            await self._store_client_data(
                websocket, client_uid, session_service_context
            )

            await self._send_initial_messages(
                websocket, client_uid, session_service_context
            )

            logger.info(f"Connection established for client {client_uid}")

        except Exception as e:
            logger.error(
                f"Failed to initialize connection for client {client_uid}: {e}"
            )
            await self._cleanup_failed_connection(client_uid)
            raise

    async def _store_client_data(
        self,
        websocket: WebSocket,
        client_uid: str,
        session_service_context: ServiceContext,
    ):
        """Store client data and initialize group status"""
        self.client_connections[client_uid] = websocket
        self.client_contexts[client_uid] = session_service_context
        self.received_data_buffers[client_uid] = np.array([])

        self.chat_group_manager.client_group_map[client_uid] = ""
        await self.send_group_update(websocket, client_uid)

    async def _send_initial_messages(
        self,
        websocket: WebSocket,
        client_uid: str,
        session_service_context: ServiceContext,
    ):
        """Send initial connection messages to the client"""
        await websocket.send_text(
            json.dumps({"type": "full-text", "text": "Connection established"})
        )

        await websocket.send_text(
            json.dumps(
                {
                    "type": "set-model-and-conf",
                    "model_info": session_service_context.live2d_model.model_info,
                    "conf_name": session_service_context.character_config.conf_name,
                    "conf_uid": session_service_context.character_config.conf_uid,
                    "client_uid": client_uid,
                }
            )
        )

        await self._send_vr_agent_config(websocket, session_service_context)

        # Send initial group status
        await self.send_group_update(websocket, client_uid)

        # Start microphone
        await websocket.send_text(json.dumps({"type": "control", "text": "start-mic"}))

    async def _init_service_context(
        self, send_text: Callable, client_uid: str
    ) -> ServiceContext:
        """Initialize service context for a new session by cloning the default context"""
        session_service_context = ServiceContext()
        await session_service_context.load_cache(
            config=self.default_context_cache.config.model_copy(deep=True),
            system_config=self.default_context_cache.system_config.model_copy(
                deep=True
            ),
            character_config=self.default_context_cache.character_config.model_copy(
                deep=True
            ),
            live2d_model=self.default_context_cache.live2d_model,
            asr_engine=self.default_context_cache.asr_engine,
            tts_engine=self.default_context_cache.tts_engine,
            vad_engine=self.default_context_cache.vad_engine,
            agent_engine=self.default_context_cache.agent_engine,
            translate_engine=self.default_context_cache.translate_engine,
            mcp_server_registery=self.default_context_cache.mcp_server_registery,
            tool_adapter=self.default_context_cache.tool_adapter,
            send_text=send_text,
            client_uid=client_uid,
        )
        return session_service_context

    async def handle_websocket_communication(
        self, websocket: WebSocket, client_uid: str
    ) -> None:
        """
        Handle ongoing WebSocket communication

        Args:
            websocket: The WebSocket connection
            client_uid: Unique identifier for the client
        """
        try:
            while True:
                try:
                    data = await websocket.receive_json()
                    message_handler.handle_message(client_uid, data)
                    await self._route_message(websocket, client_uid, data)
                except WebSocketDisconnect:
                    raise
                except json.JSONDecodeError:
                    logger.error("Invalid JSON received")
                    continue
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    await websocket.send_text(
                        json.dumps({"type": "error", "message": str(e)})
                    )
                    continue

        except WebSocketDisconnect:
            logger.info(f"Client {client_uid} disconnected")
            raise
        except Exception as e:
            logger.error(f"Fatal error in WebSocket communication: {e}")
            raise

    async def _route_message(
        self, websocket: WebSocket, client_uid: str, data: WSMessage
    ) -> None:
        """
        Route incoming message to appropriate handler

        Args:
            websocket: The WebSocket connection
            client_uid: Client identifier
            data: Message data
        """
        msg_type = data.get("type")
        if not msg_type:
            logger.warning("Message received without type")
            return

        handler = self._message_handlers.get(msg_type)
        if handler:
            await handler(websocket, client_uid, data)
        else:
            if msg_type != "frontend-playback-complete":
                logger.warning(f"Unknown message type: {msg_type}")

    def has_connected_clients(self) -> bool:
        return bool(self.client_connections)

    def is_idle(self) -> bool:
        idle = all(
            task is None or task.done() for task in self.current_conversation_tasks.values()
        )
        if idle and self._room_is_primary():
            # In the room a reply waits for the current line and a game checkpoint.
            return self.room_session.conversation_slot_available()
        return idle

    def side_chat_ready(self) -> bool:
        """A build is running in the room and nobody is answering chat yet."""
        runtimes = getattr(self, "_room_runtimes", None)
        task = getattr(self, "_side_task", None)
        return bool(
            runtimes is not None
            and self._room_is_primary()
            and runtimes.building
            and (task is None or task.done())
        )

    def side_chat(self, message, wants_build: bool) -> None:
        """Answer one chat message while the build keeps going (background)."""
        runtimes = getattr(self, "_room_runtimes", None)
        if runtimes is None:
            return
        self.room_session.note_viewer_activity()

        async def go() -> None:
            try:
                await runtimes.side_reply(
                    message.author_display_name, message.text, wants_build
                )
            except Exception as exc:
                logger.warning(f"Side reply failed: {exc}")

        self._side_task = asyncio.create_task(go())

    # ------------------------------------------------------------------
    # Class mode (VR_CLASS_MODE=1): Mika teaches a course, chat joins in.
    # ------------------------------------------------------------------
    def _maybe_start_class(self, client_uid: str, websocket: WebSocket) -> None:
        from .room.class_mode import ClassEngine, class_mode_enabled
        from .room.minecraft_mode import minecraft_mode_enabled

        if minecraft_mode_enabled():
            self._maybe_start_minecraft(client_uid, websocket)
            return
        if not class_mode_enabled():
            return
        try:
            self._room_director_ready(client_uid, websocket)
            engine = self.room_session.class_engine
            if engine is None:
                engine = ClassEngine(self._room_runtimes, self.room_session)
                self.room_session.class_engine = engine
            autopilot = getattr(self, "autopilot", None)
            if autopilot is not None:
                engine.on_lesson = autopilot.lesson_started
            engine.start()
        except Exception as exc:
            logger.error(f"Class mode could not start: {exc}")

    def _maybe_start_minecraft(self, client_uid: str, websocket: WebSocket) -> None:
        """VR_MODE=minecraft: Mika and Luna play Minecraft, chat steers them."""
        from .room.minecraft_mode import MinecraftEngine

        try:
            self._room_director_ready(client_uid, websocket)
            engine = self.room_session.mode_engine
            if engine is None:
                engine = MinecraftEngine(self._room_runtimes, self.room_session)
                self.room_session.mode_engine = engine
            autopilot = getattr(self, "autopilot", None)
            if autopilot is not None:
                engine.on_start = autopilot.mode_started
                autopilot.farewell = "That's the end of today's Minecraft stream! Thanks for playing with us, see you next session 👋"
            engine.start()
        except Exception as exc:
            logger.error(f"Minecraft mode could not start: {exc}")

    def _start_autopilot(self, runtimes) -> None:
        """Title, description, channel page and the 11h55m limit (YouTube)."""
        if getattr(self, "autopilot", None) is not None or runtimes.publisher is None:
            return
        try:
            from .publishing.stream_autopilot import StreamAutopilot

            self.autopilot = StreamAutopilot(runtimes.publisher)
            self.autopilot.goodbye = self.say_goodbye
            self.autopilot.shutdown = lambda: (getattr(self, "request_shutdown", None) or (lambda: None))()
            self.autopilot.start()
        except Exception as exc:
            logger.warning(f"Stream autopilot unavailable: {exc}")

    async def end_session(self, reason: str = "stop") -> None:
        """Ctrl+C: goodbye, end the YouTube broadcast, stop and close OBS."""
        autopilot = getattr(self, "autopilot", None)
        if autopilot is not None:
            await autopilot.end_stream(reason)
        else:
            await self.say_goodbye(reason)
            from .publishing.obs_control import stop_and_close

            await stop_and_close()

    async def say_goodbye(self, reason: str = "stop") -> None:
        """Mika and Luna say bye before the server stops (Ctrl+C or the 12 hour limit)."""
        from .room.speech import INSIDE_INTERACTION

        session = self.room_session
        mode = getattr(session, "mode_engine", None)
        if mode is not None:
            await self._mode_goodbye(mode, reason)
            return
        if not session.active or not session.speech_target():
            return
        engine = session.class_engine
        if engine is not None and engine.task and not engine.task.done():
            engine.task.cancel()  # stop teaching mid line; progress is kept
        # Only the voice: a build may still hold the interaction lock.
        INSIDE_INTERACTION.set(True)
        cast = [c.id for c in session.room.characters]
        teacher = (engine.teacher if engine else None) or (cast[0] if cast else "mika")
        sidekick = next((c for c in cast if c != teacher), None)
        if reason == "limit":
            first = (
                "Whoa, we've been live for almost twelve hours! That's all for today. "
                "Goodbye for now, and see you in the next session!"
            )
        else:
            first = "That's all for today, everyone! Goodbye for now, and see you in the next session!"
        logger.info("Saying goodbye on stream")
        await session.speech.say(teacher, first)
        if sidekick:
            await session.speech.say(
                sidekick, "Bye bye! Keep practicing, and your projects are in the description!"
            )

    async def _mode_goodbye(self, mode, reason: str) -> None:
        """Minecraft and other modes: the goodbye lines, then their processes stop."""
        from .room.speech import INSIDE_INTERACTION

        session = self.room_session
        INSIDE_INTERACTION.set(True)
        hush = getattr(mode, "hush", None)
        if hush:
            hush()  # no new chat answers: the goodbye is next, not stuck behind them

        async def speak() -> None:
            cast = [c.id for c in session.room.characters] or ["mika"]
            for _ in range(16):  # the line being spoken right now finishes first (up to 8 s)
                if not session.speech.talking:
                    break
                await asyncio.sleep(0.5)
            logger.info("Saying goodbye on stream")
            for i, line in enumerate(mode.goodbye_lines(reason)):
                await session.speech.say(cast[i % len(cast)], line)

        try:
            if session.active and session.speech_target():
                # A stage that never confirms playback made this take 30 s,
                # and the stream was never ended: speech gets 25 s at most.
                try:
                    await asyncio.wait_for(speak(), timeout=25)
                except asyncio.TimeoutError:
                    logger.warning("Goodbye speech took too long, moving on")
            else:
                logger.warning("Goodbye not spoken: the stage page is not connected")
        finally:
            try:
                await asyncio.wait_for(mode.stop(), timeout=50)  # world saved, then the server stops
            except Exception as exc:
                logger.warning(f"Mode stop failed: {exc}")

    def _chat_engine(self):
        session = self.room_session
        for name in ("mode_engine", "class_engine"):
            engine = getattr(session, name, None)
            if engine is not None and engine.active:
                return engine
        return None

    def class_active(self) -> bool:
        return self._chat_engine() is not None

    def class_message(self, message, received_at: float | None = None) -> None:
        """Chat during class or a mode: a quiz answer, a question, or a message for the bots."""
        engine = self._chat_engine()
        if engine is None:
            return
        self.room_session.note_viewer_activity()
        usage.record_viewer_interaction()
        if getattr(engine, "accepts_paid", False):  # Super Chats first, members known
            kind = getattr(message, "kind", "")
            paid = str(getattr(message, "amount", "") or "") if kind == "paid" else ""
            if kind == "paid" and not paid:
                paid = "a Super Sticker"
            if kind == "member":
                paid = "a new membership"
            member = str(getattr(message, "author_type", "") or "") in ("member", "moderator", "owner")
            engine.enqueue(message.author_display_name, message.text or "", paid=paid, member=member, received_at=received_at)
            return
        engine.enqueue(message.author_display_name, message.text)

    def _room_is_primary(self) -> bool:
        primary = self._get_primary_client()
        return bool(primary and primary[0] in self.room_client_uids)

    def register_chat_service(self, service) -> None:
        """The chat service registers itself so the room can see waiting messages."""
        self._chat_service = service

    def pending_viewer_messages(self) -> bool:
        service = getattr(self, "_chat_service", None)
        try:
            return bool(service and service.buffer.get_eligible(1))
        except Exception:
            return False

    def _room_director_ready(self, client_uid: str, websocket: WebSocket) -> bool:
        from .room.runtime import RoomRuntimes

        runtimes = getattr(self, "_room_runtimes", None)
        if runtimes is None:
            runtimes = RoomRuntimes(
                self.room_session, self.default_context_cache, client_uid, websocket.send_text
            )
            self._room_runtimes = runtimes
            self.room_session.director.turn_runner = runtimes.run_turn
            self.room_session.director.teaching_intent_classifier = (
                runtimes.classify_teaching_intent
            )
            self.room_session.director.coding_action_runner = (
                runtimes.run_coding_action
            )
            self.room_session.director.pending_probe = self.pending_viewer_messages
            self._wire_code_in_public(runtimes)
        else:
            runtimes.retarget(client_uid, websocket.send_text)
        return self.room_session.director.ready

    def _wire_code_in_public(self, runtimes) -> None:
        """Browser check, one repair and publishing, exactly like the DEV Stage.

        Everything here is optional: if a piece cannot start, coding still
        works without it and the stream never stops.
        """
        try:
            from .publishing import PublicationService, PublishSettings

            settings = PublishSettings.from_env()
            runtimes.publisher = PublicationService(settings)
            logger.info(
                "Code in Public publishing: "
                + (
                    "off"
                    if not settings.enabled
                    else ("dry run" if settings.dry_run else "LIVE to GitHub")
                )
            )
        except Exception as exc:
            logger.warning(f"Code in Public: publishing unavailable: {exc}")
        self._start_autopilot(runtimes)
        if os.environ.get("VR_BROWSER_CHECK", "1").strip() in ("0", "false", "off"):
            logger.info("Code in Public: browser check off (VR_BROWSER_CHECK=0)")
            return
        base = getattr(self, "stage_base_url", "") or "http://127.0.0.1:12393"
        try:
            from .room.browser_qa import BrowserQA

            self._browser_qa = BrowserQA(base)
            runtimes.browser_check = self._browser_qa.check
            logger.info("Code in Public: web programs are checked in a real browser")
        except Exception as exc:
            logger.warning(f"Code in Public: browser check unavailable: {exc}")

    async def _process_room_message(
        self,
        message: YouTubeChatMessage,
        client_uid: str,
        websocket: WebSocket,
        max_wait_seconds: float | None,
        received_at: float | None,
    ) -> bool | None:
        """Run a viewer message through the Conversation Director.

        Returns None when the room cannot handle it, so the classic
        single-character reply runs instead (the ultimate fallback).
        """
        from .room.live_message import LiveMessage

        live = LiveMessage.from_youtube(message)
        if live.is_system or not self._room_director_ready(client_uid, websocket):
            return None
        session = self.room_session
        try:
            plan = session.director.plan(live)
        except Exception as exc:
            logger.error(f"VR Room: director planning failed, using classic reply: {exc}")
            return None
        if not plan.turns:
            return None

        settings = self._vr_agent_settings(self.client_contexts.get(client_uid) or self.default_context_cache)
        usage.record_viewer_interaction()
        session.trace(
            "message_routed",
            interaction=plan.id,
            mode=plan.decision.mode,
            reason=plan.decision.reason,
            speakers=plan.decision.speakers,
        )
        timing = runtime.latency.start(message.message_id, received_at or time.time())
        plan.timing = timing  # type: ignore[attr-defined]
        runtime.set(VRAgentState.THINKING, message.message_id)
        show_card = settings.show_comment_card
        if show_card:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "youtube-live-selected-message",
                        "active": True,
                        "id": message.message_id,
                        "author": clean_viewer_text(live.display_name, 60) or "Viewer",
                        "message": clean_viewer_text(live.text, max(20, settings.comment_card_max_chars)),
                        "paid": message.kind == "paid",
                        "amount": clean_viewer_text(message.amount, 30),
                        "to": [t.speaker for t in plan.turns[:2]],
                    }
                )
            )
        await session.speech.lock.acquire()
        session.begin_conversation()
        task = asyncio.create_task(session.director.run(plan))
        # Not keyed by the page: an OBS refresh or source switch closes that
        # page, and a disconnect must not cancel a build that is half done.
        self.current_conversation_tasks[ROOM_INTERACTION_KEY] = task

        def on_done(finished: asyncio.Task) -> None:
            timing.finished_at = time.time()
            if session.speech.lock.locked():
                session.speech.lock.release()
            session.end_conversation()
            if not finished.cancelled() and finished.exception():
                logger.error(f"VR Room: interaction failed: {finished.exception()}")
            runtime.set(VRAgentState.IDLE, "room interaction done")
            if show_card:
                asyncio.ensure_future(
                    self._safe_send(
                        websocket,
                        {"type": "youtube-live-selected-message", "active": False, "id": message.message_id},
                    )
                )

        task.add_done_callback(on_done)
        try:
            if max_wait_seconds:
                await asyncio.wait_for(asyncio.shield(task), timeout=max_wait_seconds)
            else:
                await task
        except asyncio.TimeoutError:
            pass
        except asyncio.CancelledError:
            return False
        except Exception as exc:
            logger.error(f"VR Room: interaction error: {exc}")
        return True

    def observe_live_message(self, message) -> bool:
        """Every accepted chat message, before selection. True means consumed.

        In the room, game commands and game answers are handled here and never
        reach the LLM. Without a room page this does nothing.
        """
        if not self.room_session.active or not self.room_session.has_clients():
            return False
        from .room.live_message import LiveMessage

        live = message if isinstance(message, LiveMessage) else LiveMessage.from_youtube(message)
        return self.room_session.observe_viewer_message(live)

    def _get_primary_client(self) -> tuple[str, WebSocket, ServiceContext] | None:
        """The client that should speak for the stream.

        A page opened with ?mode=live (the OBS source) wins over a developer
        page, so opening the normal UI for debugging never steals the audio.
        """
        room_uids = (
            [uid for uid in self.client_connections if uid in self.room_client_uids]
            if self.room_session.active
            else []
        )
        ordered = room_uids + [
            uid for uid in self.client_connections if uid in self.live_client_uids
        ]
        ordered += [
            uid
            for uid in self.client_connections
            if uid not in self.live_client_uids and uid not in room_uids
        ]
        for client_uid in ordered:
            context = self.client_contexts.get(client_uid)
            if context:
                return client_uid, self.client_connections[client_uid], context
        return None

    # ------------------------------------------------------------------
    # VR Agent
    # ------------------------------------------------------------------
    def get_capabilities(self, context: ServiceContext) -> CharacterCapabilities | None:
        try:
            model_info = context.live2d_model.model_info
        except Exception:
            return None
        key = f"{model_info.get('name')}|{model_info.get('url')}"
        caps = self._capabilities_cache.get(key)
        if caps is None:
            try:
                caps = load_capabilities(model_info)
            except Exception as exc:
                logger.error(f"VR Agent: failed to load capabilities: {exc}")
                return None
            self._capabilities_cache[key] = caps
        return caps

    @staticmethod
    def _vr_agent_settings(context: ServiceContext):
        try:
            return context.config.live_config.vr_agent
        except Exception:
            from .config_manager.live import VRAgentConfig

            return VRAgentConfig()

    async def _send_vr_agent_config(self, websocket: WebSocket, context: ServiceContext) -> None:
        settings = self._vr_agent_settings(context)
        caps = self.get_capabilities(context)
        payload = {
            "type": "vr-agent-config",
            "settings": {
                "overlay_title": clean_viewer_text(settings.overlay_title, 40),
                "show_comment_card": settings.show_comment_card,
                "show_state_indicator": settings.show_state_indicator,
                "comment_card_max_chars": settings.comment_card_max_chars,
                "idle_motions_enabled": settings.idle_motions_enabled,
                "idle_min_seconds": max(3.0, float(settings.idle_min_seconds)),
                "idle_max_seconds": max(
                    float(settings.idle_min_seconds) + 1.0, float(settings.idle_max_seconds)
                ),
                "idle_in_dev_mode": settings.idle_in_dev_mode,
            },
            "capabilities": caps.to_frontend() if caps else None,
            "phase": runtime.public_phase(),
            "paused": runtime.paused,
        }
        await websocket.send_text(json.dumps(payload))

    async def _handle_vr_agent_hello(
        self, websocket: WebSocket, client_uid: str, data: WSMessage
    ) -> None:
        mode = data.get("mode")
        if mode == "room":
            self.live_client_uids.discard(client_uid)
            if self.room_session.active:
                self.room_client_uids.add(client_uid)
                await self.room_session.register(client_uid, websocket.send_text)
                try:
                    # Runtimes (and the YouTube autopilot) start with the Stage,
                    # not with the first chat message.
                    self._room_director_ready(client_uid, websocket)
                except Exception as exc:
                    logger.warning(f"VR Room: runtimes not ready yet: {exc}")
                self._maybe_start_class(client_uid, websocket)
                try:
                    # Which engine and voice each character really uses (no keys).
                    self.room_session.voices.log_report()
                except Exception as exc:  # pragma: no cover - diagnostics only
                    logger.debug(f"VR Room: voice report failed: {exc}")
            else:
                # The room page falls back to the classic livestream page.
                await websocket.send_text(
                    json.dumps(
                        {
                            "type": "vr-room-config",
                            "room": {"enabled": False},
                            "problems": self.room_session.room.problems[:5],
                        }
                    )
                )
            return
        self.room_client_uids.discard(client_uid)
        self.room_session.unregister(client_uid)
        if mode == "live":
            self.live_client_uids.add(client_uid)
            logger.info(f"Client {client_uid} registered as the livestream presentation page.")
        else:
            self.live_client_uids.discard(client_uid)

    async def _handle_vr_room_client_status(
        self, websocket: WebSocket, client_uid: str, data: WSMessage
    ) -> None:
        if client_uid in self.room_client_uids:
            self.room_session.on_client_status(
                client_uid, data.get("loaded"), data.get("failed")
            )

    async def _handle_vr_room_audio_diagnostic(
        self, websocket: WebSocket, client_uid: str, data: WSMessage
    ) -> None:
        safe = {k: v for k, v in dict(data).items() if k not in {"audio", "type"}}
        logger.warning(f"SPEECH_DIAG frontend {client_uid}: {safe}")

    async def broadcast_to_all(self, payload: str) -> None:
        for websocket in list(self.client_connections.values()):
            try:
                await websocket.send_text(payload)
            except Exception:
                pass

    async def process_youtube_live_message(
        self,
        message: YouTubeChatMessage,
        max_wait_seconds: float | None = None,
        received_at: float | None = None,
    ) -> bool:
        primary_client = self._get_primary_client()
        if not primary_client:
            logger.info("YouTube Live has no connected frontend client yet; waiting.")
            return False
        if not self.is_idle():
            logger.debug("YouTube Live skipped response because character is not idle.")
            return False

        client_uid, websocket, context = primary_client
        if client_uid in self.room_client_uids and self.room_session.active:
            handled = await self._process_room_message(
                message, client_uid, websocket, max_wait_seconds, received_at
            )
            if handled is not None:
                return handled
        settings = self._vr_agent_settings(context)
        caps = self.get_capabilities(context)
        is_system = message.author_channel_id.startswith("system-")
        viewer_name = message.author_display_name or "a viewer"

        intent = NO_INTENT
        if settings.viewer_actions_enabled and not is_system:
            intent = resolve_intent(message.text, caps)
            if intent.requested:
                logger.info(
                    f"VR Agent action request '{intent.requested}' -> "
                    f"{intent.action.name if intent.action else 'unsupported'}"
                    f"{' (alternative)' if intent.is_alternative else ''}"
                )

        user_input = build_livestream_prompt(
            viewer_name=viewer_name,
            viewer_text=message.text,
            capabilities=caps,
            intent=intent,
            is_system_prompt=is_system,
        )
        metadata = {
            "source": "youtube_live",
            "from_name": clean_viewer_text(viewer_name, 60) or "Viewer",
            "viewer_display_name": viewer_name,
            "viewer_message": message.text,
            "youtube_message_id": message.message_id,
            "skip_history": is_system,
            "skip_memory": is_system,
            # Compact record for memory and the history file; the full prompt
            # with per-turn instructions is only sent once to the LLM.
            "memory_text": f"{clean_viewer_text(viewer_name, 60)}: "
            f"{clean_viewer_text(message.text, 280)}",
            "history_text": clean_viewer_text(message.text, 500),
            "history_limit": max(0, int(settings.max_history_messages)),
        }

        if not is_system:
            usage.record_viewer_interaction()
        timing = runtime.latency.start(message.message_id, received_at or time.time())
        timing.action = intent.action.name if intent.action else None
        runtime.set(VRAgentState.MESSAGE_RECEIVED, message.message_id)

        show_card = settings.show_comment_card and not is_system
        if show_card:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "youtube-live-selected-message",
                        "active": True,
                        "id": message.message_id,
                        "author": clean_viewer_text(viewer_name, 60) or "Viewer",
                        "message": clean_viewer_text(
                            message.text, max(20, settings.comment_card_max_chars)
                        ),
                        "paid": message.kind == "paid",
                        "amount": clean_viewer_text(message.amount, 30),
                    }
                )
            )
        if intent.action:
            # Only a registry name leaves the backend; the frontend resolves it
            # against the capability list it received at connect time.
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "vr-agent-action",
                        "action": intent.action.name,
                        "sync": "speech",
                        "id": message.message_id,
                    }
                )
            )

        first_audio_sent = False

        async def tracked_send(payload: str) -> None:
            nonlocal first_audio_sent
            if not first_audio_sent and payload.startswith('{"type": "audio"'):
                first_audio_sent = True
                timing.first_audio_at = time.time()
                runtime.set(
                    VRAgentState.PERFORMING_ACTION if intent.action else VRAgentState.SPEAKING,
                    message.message_id,
                )
                logger.info(
                    f"VR Agent latency: chat->voice "
                    f"{timing.first_audio_at - timing.received_at:.2f}s "
                    f"(queue {timing.selected_at - timing.received_at:.2f}s, "
                    f"generate {timing.first_audio_at - timing.selected_at:.2f}s)"
                )
            await websocket.send_text(payload)

        room_turn = client_uid in self.room_client_uids
        if room_turn:
            # One voice at a time: the reply holds the room's speaking lock and
            # pauses a running game until it finishes.
            await self.room_session.speech.lock.acquire()
            self.room_session.begin_conversation()
        runtime.set(VRAgentState.THINKING, message.message_id)
        task = asyncio.create_task(
            process_single_conversation(
                context=context,
                websocket_send=tracked_send,
                client_uid=client_uid,
                user_input=user_input,
                images=None,
                metadata=metadata,
            )
        )
        self.current_conversation_tasks[client_uid] = task

        def on_done(finished: asyncio.Task) -> None:
            timing.finished_at = time.time()
            if finished.cancelled():
                outcome = "interrupted"
            elif finished.exception():
                outcome = f"failed: {finished.exception()}"
            else:
                outcome = "done"
            runtime.set(VRAgentState.IDLE, f"response {outcome}")
            if room_turn:
                if self.room_session.speech.lock.locked():
                    self.room_session.speech.lock.release()
                self.room_session.end_conversation()
            if show_card:
                asyncio.ensure_future(
                    self._safe_send(
                        websocket,
                        {"type": "youtube-live-selected-message", "active": False, "id": message.message_id},
                    )
                )

        task.add_done_callback(on_done)
        try:
            if max_wait_seconds:
                await asyncio.wait_for(asyncio.shield(task), timeout=max_wait_seconds)
            else:
                await task
            return True
        except asyncio.TimeoutError:
            logger.debug(
                f"YouTube Live response still running after {max_wait_seconds:.1f}s; "
                "selection loop continues when the character is idle again."
            )
            return True
        except asyncio.CancelledError:
            return False
        except Exception as exc:
            logger.error(f"YouTube Live response failed: {exc}")
            runtime.set(VRAgentState.ERROR_RECOVERABLE, f"response failed: {exc}"[:200])
            return False

    @staticmethod
    async def _safe_send(websocket: WebSocket, payload: dict) -> None:
        try:
            await websocket.send_text(json.dumps(payload))
        except Exception:
            pass

    async def _handle_group_operation(
        self, websocket: WebSocket, client_uid: str, data: dict
    ) -> None:
        """Handle group-related operations"""
        operation = data.get("type")
        target_uid = data.get(
            "invitee_uid" if operation == "add-client-to-group" else "target_uid"
        )

        await handle_group_operation(
            operation=operation,
            client_uid=client_uid,
            target_uid=target_uid,
            chat_group_manager=self.chat_group_manager,
            client_connections=self.client_connections,
            send_group_update=self.send_group_update,
        )

    async def handle_disconnect(self, client_uid: str) -> None:
        """Handle client disconnection"""
        group = self.chat_group_manager.get_client_group(client_uid)
        if group:
            await handle_group_interrupt(
                group_id=group.group_id,
                heard_response="",
                current_conversation_tasks=self.current_conversation_tasks,
                chat_group_manager=self.chat_group_manager,
                client_contexts=self.client_contexts,
                broadcast_to_group=self.broadcast_to_group,
            )

        await handle_client_disconnect(
            client_uid=client_uid,
            chat_group_manager=self.chat_group_manager,
            client_connections=self.client_connections,
            send_group_update=self.send_group_update,
        )

        # Clean up other client data
        self.live_client_uids.discard(client_uid)
        self.room_client_uids.discard(client_uid)
        self.room_session.unregister(client_uid)
        self.client_connections.pop(client_uid, None)
        self.client_contexts.pop(client_uid, None)
        self.received_data_buffers.pop(client_uid, None)
        if client_uid in self.current_conversation_tasks:
            task = self.current_conversation_tasks[client_uid]
            if task and not task.done():
                task.cancel()
            self.current_conversation_tasks.pop(client_uid, None)

        # Call context close to clean up resources (e.g., MCPClient)
        context = self.client_contexts.get(client_uid)
        if context:
            await context.close()

        logger.info(f"Client {client_uid} disconnected")
        message_handler.cleanup_client(client_uid)

    async def _cleanup_failed_connection(self, client_uid: str) -> None:
        """Clean up failed connection data"""
        self.live_client_uids.discard(client_uid)
        self.room_client_uids.discard(client_uid)
        self.room_session.unregister(client_uid)
        self.client_connections.pop(client_uid, None)
        self.client_contexts.pop(client_uid, None)
        self.received_data_buffers.pop(client_uid, None)
        self.chat_group_manager.client_group_map.pop(client_uid, None)

        if client_uid in self.current_conversation_tasks:
            task = self.current_conversation_tasks[client_uid]
            if task and not task.done():
                task.cancel()
            self.current_conversation_tasks.pop(client_uid, None)

        message_handler.cleanup_client(client_uid)

    async def broadcast_to_group(
        self, group_members: list[str], message: dict, exclude_uid: str = None
    ) -> None:
        """Broadcasts a message to group members"""
        await broadcast_to_group(
            group_members=group_members,
            message=message,
            client_connections=self.client_connections,
            exclude_uid=exclude_uid,
        )

    async def send_group_update(self, websocket: WebSocket, client_uid: str):
        """Sends group information to a client"""
        group = self.chat_group_manager.get_client_group(client_uid)
        if group:
            current_members = self.chat_group_manager.get_group_members(client_uid)
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "group-update",
                        "members": current_members,
                        "is_owner": group.owner_uid == client_uid,
                    }
                )
            )
        else:
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "group-update",
                        "members": [],
                        "is_owner": False,
                    }
                )
            )

    async def _handle_interrupt(
        self, websocket: WebSocket, client_uid: str, data: WSMessage
    ) -> None:
        """Handle conversation interruption"""
        heard_response = data.get("text", "")
        context = self.client_contexts[client_uid]
        group = self.chat_group_manager.get_client_group(client_uid)

        if group and len(group.members) > 1:
            await handle_group_interrupt(
                group_id=group.group_id,
                heard_response=heard_response,
                current_conversation_tasks=self.current_conversation_tasks,
                chat_group_manager=self.chat_group_manager,
                client_contexts=self.client_contexts,
                broadcast_to_group=self.broadcast_to_group,
            )
        else:
            await handle_individual_interrupt(
                client_uid=client_uid,
                current_conversation_tasks=self.current_conversation_tasks,
                context=context,
                heard_response=heard_response,
            )

    async def _handle_history_list_request(
        self, websocket: WebSocket, client_uid: str, data: WSMessage
    ) -> None:
        """Handle request for chat history list"""
        context = self.client_contexts[client_uid]
        histories = get_history_list(context.character_config.conf_uid)
        await websocket.send_text(
            json.dumps({"type": "history-list", "histories": histories})
        )

    async def _handle_fetch_history(
        self, websocket: WebSocket, client_uid: str, data: dict
    ):
        """Handle fetching and setting specific chat history"""
        history_uid = data.get("history_uid")
        if not history_uid:
            return

        context = self.client_contexts[client_uid]
        # Update history_uid in service context
        context.history_uid = history_uid
        context.agent_engine.set_memory_from_history(
            conf_uid=context.character_config.conf_uid,
            history_uid=history_uid,
        )

        messages = [
            msg
            for msg in get_history(
                context.character_config.conf_uid,
                history_uid,
            )
            if msg["role"] != "system"
        ]
        await websocket.send_text(
            json.dumps({"type": "history-data", "messages": messages})
        )

    async def _handle_create_history(
        self, websocket: WebSocket, client_uid: str, data: WSMessage
    ) -> None:
        """Handle creation of new chat history"""
        context = self.client_contexts[client_uid]
        history_uid = create_new_history(context.character_config.conf_uid)
        if history_uid:
            context.history_uid = history_uid
            context.agent_engine.set_memory_from_history(
                conf_uid=context.character_config.conf_uid,
                history_uid=history_uid,
            )
            await websocket.send_text(
                json.dumps(
                    {
                        "type": "new-history-created",
                        "history_uid": history_uid,
                    }
                )
            )

    async def _handle_delete_history(
        self, websocket: WebSocket, client_uid: str, data: dict
    ):
        """Handle deletion of chat history"""
        history_uid = data.get("history_uid")
        if not history_uid:
            return

        context = self.client_contexts[client_uid]
        success = delete_history(
            context.character_config.conf_uid,
            history_uid,
        )
        await websocket.send_text(
            json.dumps(
                {
                    "type": "history-deleted",
                    "success": success,
                    "history_uid": history_uid,
                }
            )
        )
        if history_uid == context.history_uid:
            context.history_uid = None

    async def _handle_audio_data(
        self, websocket: WebSocket, client_uid: str, data: WSMessage
    ) -> None:
        """Handle incoming audio data"""
        audio_data = data.get("audio", [])
        if audio_data:
            self.received_data_buffers[client_uid] = np.append(
                self.received_data_buffers[client_uid],
                np.array(audio_data, dtype=np.float32),
            )

    async def _handle_raw_audio_data(
        self, websocket: WebSocket, client_uid: str, data: WSMessage
    ) -> None:
        """Handle incoming raw audio data for VAD processing"""
        context = self.client_contexts[client_uid]
        chunk = data.get("audio", [])
        if chunk:
            for audio_bytes in context.vad_engine.detect_speech(chunk):
                if audio_bytes == b"<|PAUSE|>":
                    await websocket.send_text(
                        json.dumps({"type": "control", "text": "interrupt"})
                    )
                elif audio_bytes == b"<|RESUME|>":
                    pass
                elif len(audio_bytes) > 1024:
                    # Detected audio activity (voice)
                    self.received_data_buffers[client_uid] = np.append(
                        self.received_data_buffers[client_uid],
                        np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32),
                    )
                    await websocket.send_text(
                        json.dumps({"type": "control", "text": "mic-audio-end"})
                    )

    async def _handle_conversation_trigger(
        self, websocket: WebSocket, client_uid: str, data: WSMessage
    ) -> None:
        """Handle triggers that start a conversation"""
        await handle_conversation_trigger(
            msg_type=data.get("type", ""),
            data=data,
            client_uid=client_uid,
            context=self.client_contexts[client_uid],
            websocket=websocket,
            client_contexts=self.client_contexts,
            client_connections=self.client_connections,
            chat_group_manager=self.chat_group_manager,
            received_data_buffers=self.received_data_buffers,
            current_conversation_tasks=self.current_conversation_tasks,
            broadcast_to_group=self.broadcast_to_group,
        )

    async def _handle_fetch_configs(
        self, websocket: WebSocket, client_uid: str, data: WSMessage
    ) -> None:
        """Handle fetching available configurations"""
        context = self.client_contexts[client_uid]
        config_files = scan_config_alts_directory(context.system_config.config_alts_dir)
        await websocket.send_text(
            json.dumps({"type": "config-files", "configs": config_files})
        )

    async def _handle_config_switch(
        self, websocket: WebSocket, client_uid: str, data: dict
    ):
        """Handle switching to a different configuration"""
        config_file_name = data.get("file")
        if config_file_name:
            context = self.client_contexts[client_uid]
            await context.handle_config_switch(websocket, config_file_name)
            await self._send_vr_agent_config(websocket, context)

    async def _handle_fetch_backgrounds(
        self, websocket: WebSocket, client_uid: str, data: WSMessage
    ) -> None:
        """Handle fetching available background images"""
        bg_files = scan_bg_directory()
        await websocket.send_text(
            json.dumps({"type": "background-files", "files": bg_files})
        )

    async def _handle_audio_play_start(
        self, websocket: WebSocket, client_uid: str, data: WSMessage
    ) -> None:
        """
        Handle audio playback start notification
        """
        group_members = self.chat_group_manager.get_group_members(client_uid)
        if len(group_members) > 1:
            display_text = data.get("display_text")
            if display_text:
                silent_payload = prepare_audio_payload(
                    audio_path=None,
                    display_text=display_text,
                    actions=None,
                    forwarded=True,
                )
                await self.broadcast_to_group(
                    group_members, silent_payload, exclude_uid=client_uid
                )

    async def _handle_group_info(
        self, websocket: WebSocket, client_uid: str, data: WSMessage
    ) -> None:
        """Handle group info request"""
        await self.send_group_update(websocket, client_uid)

    async def _handle_init_config_request(
        self, websocket: WebSocket, client_uid: str, data: WSMessage
    ) -> None:
        """Handle request for initialization configuration"""
        context = self.client_contexts.get(client_uid)
        if not context:
            context = self.default_context_cache

        await websocket.send_text(
            json.dumps(
                {
                    "type": "set-model-and-conf",
                    "model_info": context.live2d_model.model_info,
                    "conf_name": context.character_config.conf_name,
                    "conf_uid": context.character_config.conf_uid,
                    "client_uid": client_uid,
                }
            )
        )
        await self._send_vr_agent_config(websocket, context)

    async def _handle_heartbeat(
        self, websocket: WebSocket, client_uid: str, data: WSMessage
    ) -> None:
        """Handle heartbeat messages from clients"""
        try:
            await websocket.send_json({"type": "heartbeat-ack"})
        except Exception as e:
            logger.error(f"Error sending heartbeat acknowledgment: {e}")
