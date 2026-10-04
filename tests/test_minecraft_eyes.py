import asyncio
import json

import pytest

from src.open_llm_vtuber.room import minecraft_eyes as me
from src.open_llm_vtuber.room import minecraft_mode as mm

from .test_minecraft_mode import _live_engine


@pytest.fixture(autouse=True)
def _own_viewer_file(tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "VIEWERS_FILE", tmp_path / "viewers.json")


SEEN = {"looking": "stone_bricks 4 blocks away", "near": [{"name": "spider", "d": 6, "where": "to the left"}],
        "players": [{"name": "Luna", "d": 3, "where": "in front"}], "on": "sand", "y": 70, "time": 6000}


def test_eyes_come_from_the_game_and_cost_nothing(monkeypatch):
    async def run():
        eng = _live_engine(monkeypatch)
        await eng._heard("Mika", "[VR] sees " + json.dumps(SEEN))
        text = eng.eyes.describe_for("mika")
        assert text == ("Looking at stone bricks 4 blocks away. Creatures: spider 6 blocks to the left. "
                        "Players: Luna 3 blocks in front. Standing on sand, height 70, day.")
        told = eng.link.sent[-1]
        assert told[0] == "Mika" and "You see right now" in told[1]["message"]  # a new spider: she reacts
        n = len(eng.link.sent)
        await eng._heard("Mika", "[VR] sees " + json.dumps(SEEN))
        assert len(eng.link.sent) == n  # nothing new: no second reaction
        system, _u = eng._reply_prompt("mika", "Viewer", "hi", [])
        assert "What YOU see in the game right now: Looking at stone bricks" in system
        assert not eng.lines  # not spoken as chatter

    asyncio.run(run())


def test_what_do_you_see_asks_the_game_first(monkeypatch):
    async def run():
        eng = _live_engine(monkeypatch)
        seen_in_prompt = []

        async def command(cid, text):
            if text == "!senses":  # her bot answers from the game
                asyncio.get_running_loop().call_soon(
                    lambda: asyncio.ensure_future(eng._heard(eng.names[cid], "[VR] sees " + json.dumps(SEEN))))
            return True

        async def reply_pieces(cid, author, text, more=None):
            seen_in_prompt.append(eng.eyes.describe_for(cid))
            yield "A spider, gross!"

        eng._command = command
        eng.reply_pieces = reply_pieces
        assert me.wants_look("Mika what do you see?") and not me.wants_look("build a castle")
        eng.enqueue("MathUnlockedYT", "Mika what do you see?")
        for _ in range(100):
            await asyncio.sleep(0.01)
            if seen_in_prompt:
                break
        assert seen_in_prompt and "spider 6 blocks to the left" in seen_in_prompt[0]

    asyncio.run(run())


def test_the_senses_command_is_patched_in():
    assert "name: '!senses'" in mm.FLY_COMMANDS and "[VR] sees " in mm.FLY_COMMANDS
    assert "!testNetwork" in mm.CREATIVE_BLOCKED  # only viewers make the network read


def test_the_camera_is_brought_back_to_mika(monkeypatch):
    async def run():
        monkeypatch.setenv("VR_MINECRAFT_PLAYER", "Selwyn")
        monkeypatch.delenv("VR_MINECRAFT_CAMERA", raising=False)
        eng = _live_engine(monkeypatch)
        eng.camera_on, eng.cam_focus = True, "mika"
        sent = []

        async def rcon(cmd, reply=False):
            sent.append(cmd)
            if cmd == "data get entity Selwyn Pos":
                return "Selwyn has the following entity data: [0.0d, 70.0d, 0.0d]"
            if cmd == "data get entity Mika Pos":
                return "Mika has the following entity data: [200.0d, 80.0d, 300.0d]"  # far away
            return "ok"

        async def no_wait(_s):
            return None

        monkeypatch.setattr(mm, "rcon_command", rcon)
        monkeypatch.setattr(mm.asyncio, "sleep", no_wait)
        await eng._camera_guard()
        assert "tp Selwyn Mika" not in sent  # one look is not enough (she may be flying)
        eng._cam_guard_at -= mm.CAMERA_GUARD_EVERY + 1
        eng._cam_apart_since -= mm.CAMERA_GUARD_EVERY + 1
        await eng._camera_guard()
        assert "tp Selwyn Mika" in sent and "spectate Mika Selwyn" in sent

    asyncio.run(run())


def test_a_frozen_girl_is_unstuck_then_restarted(monkeypatch):
    async def run():
        eng = _live_engine(monkeypatch)
        stopped, commands = [], []

        async def command(cid, text):
            commands.append((cid, text))
            return True

        eng._command = command
        monkeypatch.setattr(mm, "stop_one", lambda name, t=0: stopped.append(name))
        now = mm.time.time()
        eng._seen_at = {"mika": now, "luna": now}
        eng._pos = {"mika": (1.0, 70.0, 1.0), "luna": (5.0, 70.0, 5.0)}
        eng._ordered_at = {"mika": now}  # she keeps getting work
        await eng._unfreeze()
        eng._still["mika"] = (1.0, 70.0, 1.0, now - mm.FROZEN_SECONDS - 5)
        await eng._unfreeze()
        assert ("mika", "!stop") in commands and not stopped
        assert "luna" not in eng._still  # Luna got no work: never called frozen
        eng._still["mika"] = (1.0, 70.0, 1.0, now - 2 * mm.FROZEN_SECONDS - 5)
        await eng._unfreeze()
        assert stopped == ["mindcraft"]

    asyncio.run(run())


def test_an_overloaded_server_slows_the_building(monkeypatch):
    async def run():
        eng = _live_engine(monkeypatch)

        async def rcon(cmd, reply=False):
            return ("The game is running normally\\nTarget tick rate: 20.0 per second.\\n"
                    "Average time per tick: 87.5ms (Target: 50.0ms)")

        monkeypatch.setattr(mm, "rcon_command", rcon)
        await eng._server_health()
        assert eng._slow

    asyncio.run(run())
