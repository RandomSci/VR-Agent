import asyncio
from pathlib import Path

from src.open_llm_vtuber.room.profiles import load_room
from src.open_llm_vtuber.room.session import RoomSession


ROOT = Path(__file__).resolve().parents[1]


async def _send(_text: str) -> None:
    return None


def session():
    return RoomSession(load_room(ROOT / "room", ROOT))


def test_speech_target_prefers_latest_scene1_stage_over_backup_room():
    s = session()

    async def scenario():
        await s.register("old", _send, role="scene1-stage")
        await s.register("backup", _send, role="backup-room")
        await s.register("new", _send, role="scene1-stage")
        s.on_client_status("old", ["mika", "luna"], [])
        s.on_client_status("backup", ["mika", "luna"], [])
        await asyncio.sleep(0.001)
        s.on_client_status("new", ["mika", "luna"], [])

    asyncio.run(scenario())
    assert s.speech_target()[0] == "new"


def test_stale_stage_client_removed_from_speech_routing():
    s = session()

    async def scenario():
        await s.register("old", _send, role="scene1-stage")
        await s.register("backup", _send, role="backup-room")
        s.on_client_status("old", ["mika", "luna"], [])
        s.on_client_status("backup", ["mika", "luna"], [])
        s.unregister("old")

    asyncio.run(scenario())
    assert s.speech_target()[0] == "backup"
