import asyncio

import pytest

from src.open_llm_vtuber.room import minecraft_fun as fun
from src.open_llm_vtuber.room import minecraft_mode as mm

from .test_minecraft_mode import _live_engine


@pytest.fixture(autouse=True)
def _own_viewer_file(tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "VIEWERS_FILE", tmp_path / "viewers.json")


def _rcon(monkeypatch, replies=None):
    sent = []

    async def rcon(cmd, reply=False):
        sent.append(cmd)
        for key, value in (replies or {}).items():
            if cmd.startswith(key):
                return value
        return "ok"

    monkeypatch.setattr(mm, "rcon_command", rcon)
    return sent


def test_potions_and_items(monkeypatch):
    async def run():
        eng = _live_engine(monkeypatch)
        sent = _rcon(monkeypatch, {"give Mika minecraft:unicorn": "Unknown item 'minecraft:unicorn'"})
        await eng._heard("Mika", "[VR] drinkPotion invisible")
        assert "effect give Mika minecraft:invisibility 20 0" in sent
        assert any("entity.generic.drink" in c for c in sent)
        await eng._heard("Mika", "[VR] splashPotion glowing")
        assert "effect give Luna minecraft:glowing 30 0" in sent  # her friend gets it
        await eng._heard("Mika", "[VR] drinkPotion instant_damage")  # never: harmful
        assert not any("instant_damage" in c for c in sent)
        await eng._heard("Mika", "[VR] getItem cake 3")
        assert "give Mika minecraft:cake 3" in sent
        await eng._heard("Mika", "[VR] getItem tnt 64")
        assert not any("tnt" in c for c in sent)
        await eng._heard("Mika", "[VR] getItem unicorn 1")
        told = [m[1]["message"] for m in eng.link.sent if m[0] == "Mika"]
        assert any("no item called unicorn" in t for t in told) and any("not allowed" in t for t in told)
        assert "Mika drank a potion of invisibility" in eng._memory_text()

    asyncio.run(run())


def test_viewers_can_ask_for_a_potion_or_a_race(monkeypatch):
    eng = _live_engine(monkeypatch)
    assert fun.potion_ask("Mika drink a potion of invisibility!") == "invisibility"
    assert fun.potion_ask("potion of swiftness please") == "speed"
    assert fun.potion_ask("a potion of poison") is None
    assert fun.wants_race("you two should RACE") and not fun.wants_race("embrace it")
    assert eng.creative


def test_the_race_has_a_countdown_and_a_real_winner(monkeypatch):
    async def run():
        eng = _live_engine(monkeypatch)
        eng.projects.state.update({"base": [0, 64, 0]})
        sent = _rcon(monkeypatch)
        commands = []

        async def command(cid, text):
            commands.append((cid, text))
            return True

        async def fly(cid, view, focus):
            eng._own_spot(cid, view[0], view[1] + 64, view[2], focus[0], focus[2])
            return 0.0

        async def arrive(*a):
            return None

        async def placed(cmd):
            sent.append(cmd)
            return True

        async def loaded():
            return True

        async def no_wait(_s):
            return None

        eng._command, eng._fly, eng._arrive = command, fly, arrive
        eng.projects.place_one, eng.projects.ensure_loaded = placed, loaded
        monkeypatch.setattr(mm.asyncio, "sleep", no_wait)
        now = mm.time.time()
        eng._seen_at = {"mika": now, "luna": now}
        eng.fun.levels["luna"] = 2  # Luna drank a speed potion before
        await eng.fun.race("mika")
        titles = [c for c in sent if c.startswith("title @a title")]
        assert [t.split('"text":"')[1].split('"')[0] for t in titles] == ["3", "2", "1", "GO!", "Luna wins!"]
        assert sum(1 for c in sent if c.startswith("fill ")) == 4 * len(fun.HOOPS)  # the hoops
        go = [t for c, t in commands if t.startswith("!flyTo(")]
        assert {c for c, t in commands} == {"mika", "luna"} and len(go) == 2
        lanes = {float(t.split(", ")[2]) for t in go}
        assert len(lanes) == 2  # side by side, each in her own lane
        assert "Luna won the sky race against Mika" in eng._memory_text()
        assert not eng.fun.racing and eng._chose_at["mika"] < mm.time.time() - 30  # building can go on
        await eng.fun.race("mika")  # the course is built only once
        assert sum(1 for c in sent if c.startswith("fill ")) == 4 * len(fun.HOOPS)

    asyncio.run(run())


def test_luna_flies_in_front_of_mikas_eyes(monkeypatch):
    async def run():
        eng = _live_engine(monkeypatch)
        _rcon(monkeypatch, {"data get entity Mika Rotation": "Mika has the following entity data: [90.0f, 5.0f]"})
        sent = []

        async def command(cid, text):
            sent.append((cid, text))
            return True

        eng._command = command
        eng._pos = {"mika": (10.0, 70.0, 10.0), "luna": (30.0, 70.0, 30.0)}
        await eng._heard("Mika", "[VR] standInFront")  # Mika is the camera: Luna comes
        cid, text = sent[-1]
        x, _y, z = (float(v) for v in text[len("!flyTo("):].split(", ")[:3])
        assert cid == "luna" and round(x) == 5 and round(z) == 10  # yaw 90: 5 blocks toward -x

    asyncio.run(run())
