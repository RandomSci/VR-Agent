"""Viewer input through the REAL chat path, not direct game calls.

YouTube message -> YouTubeLiveChatService._accept (the entry the Playwright
reader and the API poller both use) -> safety checks -> the room's active
context router -> the active game -> authoritative game state -> the
vr-room-update ops the OBS page draws.

Regression for the live bug: as X on chat's turn, typing "5" did nothing.
Root cause: the reply buffer rejected any message of one character as
"noise" (and the third identical vote as "repeated_spam") BEFORE the room
could see it. Games now get the first look.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

handler_mod = pytest.importorskip("open_llm_vtuber.websocket_handler")

from open_llm_vtuber.games.rps.game import parse_hand  # noqa: E402
from open_llm_vtuber.games.tictactoe.game import parse_cell  # noqa: E402
from open_llm_vtuber.live.youtube_live import (  # noqa: E402
    YouTubeChatMessage,
    YouTubeLiveChatService,
    YouTubeMessageBuffer,
)
from open_llm_vtuber.room.runtime import RoomRuntimes  # noqa: E402
from open_llm_vtuber.vr_agent.state import runtime  # noqa: E402
from open_llm_vtuber.vr_agent.text_safety import (  # noqa: E402
    VIEWER_TEXT_MAX,
    is_garbage,
)
from open_llm_vtuber.vr_agent.usage import usage  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class RoomSocket:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def ops(self, kind: str) -> list[dict]:
        return [
            op
            for m in self.sent
            if m.get("type") == "vr-room-update"
            for op in m["ops"]
            if op.get("op") == kind
        ]


def yt(text: str, author: str = "Selwyn") -> YouTubeChatMessage:
    return YouTubeChatMessage(
        message_id=f"yt-{time.time_ns()}",
        author_channel_id=f"UC-{author}",
        author_display_name=author,
        text=text,
        timestamp=datetime.now(timezone.utc),
    )


def service_config():
    return SimpleNamespace(
        youtube_live_enabled=True,
        chat_source="api",
        api_key="",
        channel_id="",
        video_id=None,
        prefer_stream_list=True,
        max_buffer_messages=100,
        message_buffer_seconds=180,
        same_user_cooldown_seconds=90,
        response_cooldown_seconds=1,
        selector_max_messages=12,
        idle_banter_enabled=False,
        idle_banter_delay_seconds=0,
        discovery_retry_seconds=10,
    )


@pytest.fixture
def live(monkeypatch):
    monkeypatch.chdir(ROOT)
    usage.reset()
    runtime.paused = False
    monkeypatch.setattr(RoomRuntimes, "agent", lambda self, cid: object())
    monkeypatch.setattr(RoomRuntimes, "_live2d", lambda self, cid: SimpleNamespace())
    handler = handler_mod.WebSocketHandler(SimpleNamespace())
    sock = RoomSocket()
    handler.client_connections = {"room": sock}
    handler.client_contexts = {"room": SimpleNamespace()}
    service = YouTubeLiveChatService(service_config(), None, handler)
    return handler, sock, service


async def _open_room(handler, sock):
    await handler._handle_vr_agent_hello(
        sock, "room", {"type": "vr-agent-hello", "mode": "room"}
    )
    handler.room_session.on_client_status("room", ["mika", "luna"], [])


async def _wait(cond, timeout=20.0):
    end = time.time() + timeout
    while not cond():
        if time.time() > end:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.05)


async def _start_tictactoe(handler, sock, service):
    await _open_room(handler, sock)
    assert service._accept(yt("Luna, play tic tac toe with us"), "message")
    session = handler.room_session
    game = session.show.engine.active
    assert game is not None and game.info.id == "tictactoe"
    game.think_s = 0.2
    game.quiet_close_s = 1.0
    session.show.lines.clear()  # skip the spoken intro, not what we test
    await _wait(lambda: game.phase == "vote" and game.turn == "viewers")
    return session, game


def test_five_on_chats_turn_places_x_in_the_center(live):
    handler, sock, service = live

    async def scenario():
        session, game = await _start_tictactoe(handler, sock, service)
        assert game.marks["viewers"] == "X"
        assert service._accept(yt("5"), "message") is True  # consumed by the game
        assert game.vote.counts().get("4") == 1
        await _wait(lambda: game.cells[4] == "X")
        await asyncio.sleep(0.4)
        return session, game

    session, game = asyncio.run(scenario())
    boards = [op["view"] for op in sock.ops("board") if op.get("view")]
    assert any(
        ((v.get("body") or {}).get("cells") or [{}] * 9)[4].get("mark") == "X"
        for v in boards
    ), "the OBS page was never told about the X"
    assert session.show.engine.active is game
    assert usage.snapshot()["llm_requests"] == 0


@pytest.mark.parametrize(
    "text",
    [
        "5",
        "center",
        "middle",
        "middle square",
        "put X in 5",
        "I choose 5",
        "five",
        "#5",
        "cell 5",
        "5 please",
    ],
)
def test_natural_move_words_reach_the_center_cell(live, text):
    handler, sock, service = live

    async def scenario():
        session, game = await _start_tictactoe(handler, sock, service)
        assert service._accept(yt(text), "message") is True
        await _wait(lambda: game.cells[4] == "X")

    asyncio.run(scenario())


def test_a_crowd_voting_the_same_cell_is_never_dropped_as_spam(live):
    handler, sock, service = live

    async def scenario():
        session, game = await _start_tictactoe(handler, sock, service)
        game.quiet_close_s = 5
        for name in ("Selwyn", "Ana", "Ben", "Cara", "Dan"):
            assert service._accept(yt("5", name), "message") is True
        return game

    game = asyncio.run(scenario())
    assert game.vote.counts().get("4") == 5


def test_occupied_and_invalid_cells_do_not_change_the_board(live):
    handler, sock, service = live

    async def scenario():
        session, game = await _start_tictactoe(handler, sock, service)
        game.cells[4] = "O"  # pretend Luna already took the center
        assert service._accept(yt("5"), "message") is True  # swallowed, not placed
        assert game.vote.counts().get("4") is None
        before = list(game.cells)
        service._accept(yt("10"), "message")  # not a cell: ordinary chat
        service._accept(yt("0"), "message")
        assert game.cells == before and game.vote.counts() == {}
        queued = [m.text for m in service.buffer.messages]
        assert "10" in queued  # went on to conversation, not the game
        return game

    game = asyncio.run(scenario())
    assert game.cells[4] == "O"


def test_five_outside_a_game_changes_nothing(live):
    handler, sock, service = live

    async def scenario():
        await _open_room(handler, sock)
        service._accept(yt("5"), "message")
        return handler.room_session.show.engine.active

    assert asyncio.run(scenario()) is None
    assert handler.room_session.show.engine.active is None
    assert sock.ops("board") == [] or all(
        not op.get("view") for op in sock.ops("board")
    )


def test_rps_votes_arrive_through_the_same_path(live):
    handler, sock, service = live

    async def scenario():
        await _open_room(handler, sock)
        assert service._accept(yt("Mika, play rock paper scissors with us"), "message")
        session = handler.room_session
        game = session.show.engine.active
        session.show.lines.clear()
        await _wait(lambda: game.phase == "vote")
        for name, text in (
            ("Selwyn", "rock"),
            ("Ana", "rock"),
            ("Ben", "rock"),  # third identical vote: used to be "repeated_spam"
            ("Cara", "I choose paper"),
        ):
            assert service._accept(yt(text, name), "message") is True
        return game

    game = asyncio.run(scenario())
    assert game.vote.counts() == {"rock": 3, "paper": 1}


def test_parsers_accept_natural_moves_but_leave_chat_alone():
    assert parse_cell("top left corner") == 0
    assert parse_cell("left top") == 0
    assert parse_cell("X on the bottom right please") == 8
    assert parse_cell("I think 5 people watch") is None
    assert parse_cell("you should go left, trust me bro") is None
    assert parse_cell("55") is None
    assert parse_hand("going with scissors please") == "scissors"
    assert parse_hand("rock is overrated tbh") is None
    assert parse_hand("✊") == "rock"


def test_a_full_100_character_comment_reaches_the_director_prompt(live):
    handler, sock, service = live
    text = (
        "Luna what is your favourite star and why do you like it so much tell me "
        "everything please ok thank you"
    )[:VIEWER_TEXT_MAX]
    text = text.ljust(VIEWER_TEXT_MAX, "!")
    assert len(text) == VIEWER_TEXT_MAX

    async def scenario():
        await _open_room(handler, sock)
        assert service._accept(yt(text), "message") is True  # queued for a reply
        queued = list(service.buffer.messages)
        from open_llm_vtuber.room.live_message import LiveMessage

        live_msg = LiveMessage.from_youtube(queued[-1])
        plan = handler.room_session.director.plan(live_msg)
        prompt = handler.room_session.director._prompt(plan, 0)
        return queued[-1].text, live_msg.clean_text, prompt

    stored, clean, prompt = asyncio.run(scenario())
    assert stored == text and clean == text
    assert text.replace('"', "'") in prompt


def test_long_comments_are_trimmed_once_and_garbage_is_rejected():
    buffer = YouTubeMessageBuffer(100, 180, 90)
    long_text = "word " * 40
    ok, reason, msg = buffer.admit(yt(long_text))
    assert ok and len(msg.text) <= VIEWER_TEXT_MAX and msg.text.endswith("…")
    ok, reason, _ = buffer.admit(yt("W" * 60))
    assert not ok and reason == "garbage"
    assert is_garbage("!!!!!!!!!!!!!!!!!!")
    assert is_garbage("lolololololololol")
    assert (
        not is_garbage("5") and not is_garbage("rock") and not is_garbage("hello there")
    )
    # Conversation filters still apply to what the room does not consume.
    ok, reason, msg = buffer.admit(yt("5"))
    assert ok
    assert buffer.queue(msg) == (False, "noise")
