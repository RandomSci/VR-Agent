"""The WebSocket handler with a room page: chat goes through the Director, the
reply is tagged with the speaking character, game commands never reach the
LLM, and the classic page path is unchanged when no room page is open."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

handler_mod = pytest.importorskip("open_llm_vtuber.websocket_handler")

from open_llm_vtuber.conversations import single_conversation  # noqa: E402
from open_llm_vtuber.live.youtube_live import YouTubeChatMessage  # noqa: E402
from open_llm_vtuber.message_handler import message_handler  # noqa: E402
from open_llm_vtuber.room.runtime import RoomRuntimes  # noqa: E402
from open_llm_vtuber.vr_agent.state import runtime  # noqa: E402
from open_llm_vtuber.vr_agent.usage import usage  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class RoomSocket:
    """Acts like room.js: reports playback complete after backend-synth-complete."""

    def __init__(self, uid: str):
        self.uid = uid
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        payload = json.loads(text)
        self.sent.append(payload)
        if payload.get("type") == "backend-synth-complete":
            asyncio.get_running_loop().call_soon(
                message_handler.handle_message,
                self.uid,
                {"type": "frontend-playback-complete"},
            )

    def of_type(self, kind: str) -> list[dict]:
        return [m for m in self.sent if m.get("type") == kind]


def message(text: str, author: str = "@ana") -> YouTubeChatMessage:
    return YouTubeChatMessage(
        message_id=f"id-{time.time_ns()}",
        author_channel_id=f"UC{author}",
        author_display_name=author,
        text=text,
        timestamp=datetime.now(timezone.utc),
    )


@pytest.fixture
def room_handler(monkeypatch):
    monkeypatch.chdir(ROOT)
    usage.reset()
    runtime.paused = False
    prompts: list[str] = []

    async def fake_conversation(
        context, websocket_send, client_uid, user_input, images, metadata
    ):
        usage.record_llm("fake")
        prompts.append(user_input)
        assert metadata["history_limit"] == -1 and metadata["skip_memory"] is True
        text = f"{context.character_config.character_name} answers."
        await websocket_send(
            json.dumps(
                {
                    "type": "audio",
                    "audio": "UklGRg==",
                    "volumes": [0.5],
                    "slice_length": 20,
                    "display_text": {
                        "text": text,
                        "name": context.character_config.character_name,
                    },
                    "actions": {"expressions": [1]},
                }
            )
        )
        await websocket_send(json.dumps({"type": "backend-synth-complete"}))
        return text

    monkeypatch.setattr(
        single_conversation, "process_single_conversation", fake_conversation
    )
    monkeypatch.setattr(RoomRuntimes, "agent", lambda self, cid: object())
    monkeypatch.setattr(RoomRuntimes, "_live2d", lambda self, cid: SimpleNamespace())
    handler = handler_mod.WebSocketHandler(SimpleNamespace())
    sock = RoomSocket("room")
    handler.client_connections = {"room": sock}
    handler.client_contexts = {"room": SimpleNamespace()}
    return handler, sock, prompts


def test_named_character_answers_with_her_own_tagged_audio(room_handler):
    handler, sock, prompts = room_handler

    async def scenario():
        await handler._handle_vr_agent_hello(
            sock, "room", {"type": "vr-agent-hello", "mode": "room"}
        )
        handler.room_session.on_client_status("room", ["mika", "luna"], [])
        ok = await handler.process_youtube_live_message(
            message("Hey Luna, what do you think about stars?")
        )
        await asyncio.sleep(0.05)
        return ok

    assert asyncio.run(scenario()) is True
    audio = sock.of_type("audio")
    assert (
        audio
        and audio[0]["character"] == "luna"
        and audio[0]["emotion_mode"] == "profile"
    )
    card = sock.of_type("youtube-live-selected-message")[0]
    assert card["to"][0] == "luna"
    assert "as Luna" in prompts[0] and "YouTube viewer @ana says" in prompts[0]
    snapshot = usage.snapshot()
    assert snapshot["llm_requests"] == len(prompts) >= 1
    assert snapshot["viewer_triggered_interactions"] == 1
    assert not handler.room_session.speech.lock.locked()
    updates = [op for m in sock.of_type("vr-room-update") for op in m["ops"]]
    assert any(
        op["op"] == "attention" for op in updates
    )  # the listener looks at the speaker


def test_game_commands_are_consumed_without_llm(room_handler):
    handler, sock, prompts = room_handler

    async def scenario():
        await handler._handle_vr_agent_hello(
            sock, "room", {"type": "vr-agent-hello", "mode": "room"}
        )
        handler.room_session.on_client_status("room", ["mika", "luna"], [])
        consumed = handler.observe_live_message(message("what games can you play?"))
        await asyncio.sleep(0.2)
        return consumed

    assert asyncio.run(scenario()) is True
    assert prompts == []
    assert usage.snapshot()["llm_requests"] == 0


def test_without_a_room_page_nothing_is_consumed(room_handler):
    handler, sock, prompts = room_handler
    assert handler.observe_live_message(message("play trivia")) is False


def test_inherited_voice_uses_the_engine_loaded_after_startup(monkeypatch):
    """Regression: the server builds the handler before conf.yaml's TTS exists.

    Mika's voice inherits conf.yaml TTS; it must be looked up when she speaks,
    not captured (as None) at startup.
    """
    monkeypatch.chdir(ROOT)
    context = SimpleNamespace(character_config=None, tts_engine=None)
    handler = handler_mod.WebSocketHandler(context)
    voices = handler.room_session.voices
    assert voices.engine("mika") is None  # not loaded yet, and not cached as None
    engine = object()
    context.character_config = SimpleNamespace(tts_config=None)
    context.tts_engine = engine
    assert voices.engine("mika") is engine
    replacement = object()  # a config switch replaces the engine
    context.tts_engine = replacement
    assert voices.engine("mika") is replacement
