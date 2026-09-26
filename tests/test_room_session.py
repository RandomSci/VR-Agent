"""RoomSession ops, attention targets and the WebSocket handler integration."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from open_llm_vtuber.room.profiles import RoomConfig, load_room
from open_llm_vtuber.room.session import RoomSession
from open_llm_vtuber.room.state import AttentionTarget

ROOT = Path(__file__).resolve().parents[1]


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    def of_type(self, kind):
        return [m for m in self.sent if m.get("type") == kind]


def session():
    return RoomSession(load_room(ROOT / "room", ROOT))


@pytest.mark.parametrize(
    "value,expected",
    [
        ("VIEWER", "VIEWER"),
        ("viewer", "VIEWER"),
        ("GAME", "GAME"),
        ("CHARACTER:luna", "CHARACTER:luna"),
        ("OBJECT:game_board", "OBJECT:game_board"),
    ],
)
def test_attention_targets_parse(value, expected):
    assert str(AttentionTarget.parse(value)) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "CHARACTER",
        "CHARACTER:../x",
        "OBJECT:Game Board",
        "VIEWER:x",
        "SELF",
        "OBJECT:a" * 40,
    ],
)
def test_invalid_attention_targets_are_rejected(value):
    with pytest.raises(ValueError):
        AttentionTarget.parse(value)


def test_attention_op_is_validated_and_updates_state():
    s = session()
    op = s.attention_op("mika", "CHARACTER:luna", hold_seconds=999, delay_seconds=-3)
    assert op == {
        "op": "attention",
        "character": "mika",
        "target": "CHARACTER:luna",
        "hold_ms": 60000,
        "delay_ms": 0,
    }
    assert str(s.state.characters["mika"].attention) == "CHARACTER:luna"
    assert s.attention_op("mika", "CHARACTER:mika") is None  # not at herself
    assert s.attention_op("mika", "CHARACTER:nobody") is None
    assert s.attention_op("mika", "OBJECT:unknown_thing") is None
    assert s.attention_op("nobody", "VIEWER") is None
    assert s.attention_op("luna", "OBJECT:game_board")["target"] == "OBJECT:game_board"


def test_action_op_only_allows_registry_actions():
    s = session()
    assert s.action_op("luna", "cheer")["name"] == "cheer"
    assert s.action_op("luna", "summon_rabbit") is None  # Mika's, not Luna's
    assert s.action_op("mika", "alert(1)") is None


def test_register_sends_config_and_availability_follows_client_status():
    s = session()
    sock = FakeSocket()

    async def scenario():
        await s.register("page", sock.send_text)
        s.on_client_status("page", ["mika"], ["luna"])
        await s.push([s.attention_op("mika", "VIEWER"), {"op": "exec", "code": "x"}])

    asyncio.run(scenario())
    (config,) = sock.of_type("vr-room-config")
    assert [c["id"] for c in config["room"]["characters"]] == ["mika", "luna"]
    assert s.state.available_characters() == ["mika"]
    (update,) = sock.of_type("vr-room-update")
    assert [op["op"] for op in update["ops"]] == ["attention"]  # unknown op dropped
    s.unregister("page")
    assert s.state.available_characters() == ["mika", "luna"]


# ---------------------------------------------------------------------------
# WebSocketHandler integration
# ---------------------------------------------------------------------------
handler_mod = pytest.importorskip("open_llm_vtuber.websocket_handler")


def build_handler(monkeypatch, room=None):
    monkeypatch.chdir(ROOT)
    handler = handler_mod.WebSocketHandler(SimpleNamespace())
    if room is not None:
        handler.room_session = RoomSession(room)
    return handler


def test_room_page_is_registered_and_preferred_for_the_stream(monkeypatch):
    handler = build_handler(monkeypatch)
    dev, live, page = FakeSocket(), FakeSocket(), FakeSocket()
    handler.client_connections = {"dev": dev, "live": live, "room": page}
    handler.client_contexts = {"dev": object(), "live": object(), "room": object()}

    async def scenario():
        await handler._handle_vr_agent_hello(live, "live", {"mode": "live"})
        await handler._handle_vr_agent_hello(page, "room", {"mode": "room"})
        await handler._handle_vr_room_client_status(
            page, "room", {"loaded": ["mika", "luna"], "failed": []}
        )

    asyncio.run(scenario())
    assert page.of_type("vr-room-config")[0]["room"]["enabled"] is True
    assert handler._get_primary_client()[0] == "room"

    # Without the room page the classic livestream page speaks again.
    handler.room_client_uids.discard("room")
    assert handler._get_primary_client()[0] == "live"


def test_room_page_gets_disabled_config_when_room_is_off(monkeypatch):
    handler = build_handler(monkeypatch, RoomConfig())
    page = FakeSocket()
    handler.client_connections = {"room": page}
    handler.client_contexts = {"room": object()}
    asyncio.run(handler._handle_vr_agent_hello(page, "room", {"mode": "room"}))
    assert page.of_type("vr-room-config")[0]["room"] == {"enabled": False}
    assert "room" not in handler.room_client_uids
    # Classic mode keeps working: the page is still an ordinary client.
    assert handler._get_primary_client()[0] == "room"
