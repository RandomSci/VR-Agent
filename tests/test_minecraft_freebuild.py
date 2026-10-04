import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from src.open_llm_vtuber.room import minecraft_freebuild as fb
from src.open_llm_vtuber.room import minecraft_fun as fun
from src.open_llm_vtuber.room import minecraft_mode as mm
from src.open_llm_vtuber.room import minecraft_projects as mp

from .test_minecraft_mode import _live_engine


@pytest.fixture(autouse=True)
def _own_viewer_file(tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "VIEWERS_FILE", tmp_path / "viewers.json")
    monkeypatch.setenv("VR_MODERATION", "0")  # no test calls the real moderation service
    from src.open_llm_vtuber.room import minecraft_projects as _mp

    monkeypatch.setattr(_mp, "STATE_FILE", tmp_path / "project.json")  # never the real saved world progress


SKY_CASTLE = {
    "title": "Selwyn's sky castle",
    "parts": [
        {"from": [-4, 10, 2], "to": [4, 10, 10], "block": "quartz_block", "shape": "solid"},
        {"from": [-4, 11, 2], "to": [4, 14, 10], "block": "minecraft:stone_bricks", "shape": "walls"},
        {"from": [0, 0, 0], "to": [0, 0, 0], "block": "lava", "shape": "solid"},
    ],
    "text": {"words": "Selwyn!", "block": "gold_block", "y": 11, "z": 2},
}


def test_a_design_is_checked_and_kept_small():
    design = fb.parse_design("Sure! " + json.dumps(SKY_CASTLE))
    assert design["parts"][1]["block"] == "stone_bricks"
    assert design["parts"][2]["block"] == fb.FALLBACK_BLOCK  # never lava: it would burn the builds
    huge = fb.parse_design(json.dumps({"title": "huge", "parts": [
        {"from": [-99, 0, 0], "to": [99, 999, 999], "block": "tnt", "shape": "walls"}]}))
    assert huge["parts"][0]["block"] == "tnt"  # no limits on what they build with
    assert huge["parts"][0]["from"] == [-fb.SIZE_X, 0, 0] and huge["parts"][0]["to"][1] == fb.SIZE_Y
    assert len(fb.blocks_of(huge)) == fb.MAX_BLOCKS
    assert design["text"]["words"] == "SELWYN"
    assert fb.parse_design("no json here") is None
    blocks = fb.blocks_of(design)
    assert len(blocks) <= fb.MAX_BLOCKS
    walls = fb.blocks_of({"title": "t", "parts": [design["parts"][1]], "text": None})
    assert ((0, 12, 6), "stone_bricks") not in walls and ((-4, 12, 6), "stone_bricks") in walls  # hollow


def test_letters_read_left_to_right_for_the_builders():
    design = {"title": "t", "parts": [], "text": {"words": "LI", "block": "gold_block", "y": 0, "z": 0}}
    spots = {p for p, _b in fb.blocks_of(design)}
    # L: its long upright is on the left (smaller x), I comes after it to the right
    assert all(((-3, y, -1) in spots) for y in range(0, 5))
    assert max(x for x, _y, _z in spots) == 3
    # facing south (+z) her right hand is west (-x): letters stay readable from where she stands
    assert fb.to_world((1, 0, 0), (0, 1)) == (-1, 0, 0) and fb.to_world((0, 0, 1), (0, 1)) == (0, 0, 1)
    assert fb.to_world((1, 0, 0), (1, 0)) == (0, 0, 1)


def test_viewers_can_ask_in_their_own_words():
    assert fun.potion_ask("drink invisible potion mika") == "invisibility"
    assert fun.potion_ask("Luna splash Mika with glowing") == "glowing"
    assert fun.potion_ask("Mika drink a potion of invisibility!") == "invisibility"
    assert fun.potion_ask("drink some water") is None
    assert fb.wants_build("build sky castle now Mika and luna")
    assert fb.wants_build("Mika place a block of stone on the sand.")
    assert not fb.wants_build("how are you guys?")


class _DesignLLM:
    def __init__(self, design):
        self.design = design
        self.chat = NS(completions=NS(create=self.create))

    async def create(self, **kwargs):
        return NS(choices=[NS(message=NS(content=json.dumps(self.design)))])


def test_a_viewer_build_is_laid_by_hand_in_front_of_the_camera(monkeypatch):
    async def run():
        eng = _live_engine(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        eng._llm = _DesignLLM(SKY_CASTLE)
        eng.projects.state.update({"base": [0, 64, 0]})
        now = mm.time.time()
        eng._seen_at = {"mika": now, "luna": now}
        eng._pos = {"mika": (0.5, 64.0, 0.5), "luna": (3.5, 64.0, 0.5)}
        eng._looking["mika"] = (0.5, 20.5, now)  # she looks south (+z)
        rcon, placed, lays = [], [], []

        async def fake_rcon(cmd, reply=False):
            rcon.append(cmd)
            if cmd.startswith("execute unless block"):  # the ground is at y 63
                return "Test passed" if int(cmd.split()[4]) <= 63 else "Test failed"
            return "ok"

        async def command(cid, text):
            if text.startswith("!layBlocks("):
                job = int(text[len("!layBlocks("):].split(",")[0])
                lays.append((cid, text))
                for i in range(len(text.rsplit('"', 2)[-2].split(";"))):
                    await eng._hand_event(["put", str(job), str(i)])
                await eng._hand_event(["laid", str(job)])
            return True

        async def place_one(cmd):
            placed.append(cmd)
            return True

        async def loaded():
            return True

        async def arrive(*a):
            return None

        async def no_wait(_s):
            return None

        real_sleep = asyncio.sleep
        monkeypatch.setattr(mm, "rcon_command", fake_rcon)
        monkeypatch.setattr(mm.asyncio, "sleep", no_wait)
        eng._command, eng._arrive = command, arrive
        eng.projects.place_one, eng.projects.ensure_loaded = place_one, loaded
        eng.projects.state["plots"] = 0  # the first plot
        note = eng.request_build("build a sky castle with my name Selwyn on it", "MathUnlockedYT")
        assert "by hand" in note
        for _ in range(500):
            await real_sleep(0.005)
            if not eng._free_busy and not eng._free_waiting:
                break
        expected = len(fb.blocks_of(fb.parse_design(json.dumps(SKY_CASTLE))))
        sets = [c for c in rcon if c.startswith("setblock ")]
        assert not placed  # nothing just appeared
        assert len(sets) == len(set(sets)) == expected  # every block once, each on a swing
        assert {c for c, _t in lays} == {"mika", "luna"}  # both build
        # on a Kingdom lot of its own next to where they work (never on the base projects), on the ground
        base = eng.projects.state["base"]
        xs = [int(c.split()[1]) - base[0] for c in sets]
        zs = [int(c.split()[3]) - base[2] for c in sets]
        ys = [int(c.split()[2]) for c in sets]
        assert eng.projects.state["kingdom"]["claimed"] and min(ys) >= 64
        p1, q1, p2, q2 = mp.LOADED
        assert max(xs) < p1 or min(xs) > p2 or max(zs) < q1 or min(zs) > q2
        assert any(c.startswith("fill ") and c.endswith("minecraft:air") for c in rcon)  # the plot was cleared first
        assert any(c.startswith("forceload add") for c in rcon)  # its ground was loaded before it was measured
        assert any(c.startswith("forceload remove") for c in rcon)  # and let go when it was done
        assert len(eng.projects.state["areas"]) == 2  # the projects area and this plot are taken
        assert "MathUnlockedYT" in eng._memory_text() and "finished" in eng._memory_text()
        assert any(op.get("kind") == "project_done" for op in eng.pushed)

    asyncio.run(run())


def test_the_camera_stays_behind_where_she_looks(monkeypatch):
    async def run():
        monkeypatch.setenv("VR_MINECRAFT_PLAYER", "Selwyn")
        monkeypatch.setenv("VR_MINECRAFT_CAMERA", "behind")
        eng = _live_engine(monkeypatch)

        async def rcon(cmd, reply=False):
            if cmd == "data get entity Mika Pos":
                return "Mika has the following entity data: [0.0d, 70.0d, 0.0d]"
            if cmd == "data get entity Mika Rotation":
                return "Mika has the following entity data: [0.0f, 0.0f]"  # the server says south...
            return "ok"

        monkeypatch.setattr(mm, "rcon_command", rcon)
        sent = []

        async def emit(event, *args):
            sent.append(args)
            return True

        eng.link.emit = emit
        await eng._command("mika", "!flyTo(0.0, 70.0, 0.0, -20.0, 70.0, 0.0, -60)")  # ...we told her: look west
        eng.cam_focus = "mika"
        await eng._chase_target()
        (cx, _cy, cz), (lx, _ly, lz) = eng._shot
        assert cx > 0 and abs(cz) < 0.01  # behind her is east
        assert lx < 0  # looking west, past her

    asyncio.run(run())


def test_digging_is_done_by_hand_with_a_pickaxe_from_the_top_down():
    design = fb.parse_design(json.dumps({"title": "A hole", "parts": [
        {"from": [-1, -3, 3], "to": [1, -1, 5], "block": "air", "shape": "solid"}]}))
    spots = fb.blocks_of(design)
    assert len(spots) == 27 and all(b == "air" for _p, b in spots)
    assert spots[0][0][1] == -1 and spots[-1][0][1] == -3  # top layer first
    steps = fb.pieces(design, (0, 64, 0), (0, 64, 0), (0, 1))
    commands = [c for st in steps for c in st["commands"]]
    from src.open_llm_vtuber.room.minecraft_projects import place

    absolute = [place(c, (0, 64, 0), {}) for c in commands]
    assert all(mm.hand_blocks(a) for a in absolute)  # every dig is by hand
    assert mm.hand_item("minecraft:air") == "diamond_pickaxe"
    assert mm.hand_blocks("fill 0 0 0 40 10 40 minecraft:air") is None  # a big site clearing just happens
    assert fb.wants_build("dig a hole here mika")


def test_tnt_never_near_the_builds(monkeypatch):
    async def run():
        eng = _live_engine(monkeypatch)
        eng.projects.state.update({"base": [0, 64, 0]})
        rcon, hand = [], []

        async def fake_rcon(cmd, reply=False):
            rcon.append(cmd)
            return "ok"

        async def lay(cid, run, hover=None, look=None):
            hand.append((cid, run))
            return True

        monkeypatch.setattr(mm, "rcon_command", fake_rcon)
        eng._lay_by_hand = lay
        eng._pos = {"mika": (10.0, 64.0, 10.0), "luna": (12.0, 64.0, 10.0)}
        eng._looking["mika"] = (10.0, 30.0, mm.time.time())
        flights = []

        async def fly(cid, view, focus):
            flights.append((cid, view))
            return 0.0

        async def arrive(cid, view, focus, t):
            return None

        async def no_wait(_s):
            return None

        real_sleep = asyncio.sleep
        monkeypatch.setattr(mm.asyncio, "sleep", no_wait)
        eng._fly, eng._arrive = fly, arrive
        await eng._heard("Mika", "[VR] blowUp")
        await real_sleep(0.02)  # it runs in the background
        # next to the base: she flies to the blast range first, and it goes off there
        assert flights and flights[0][1][0] == fun.TNT_RANGE[0] and flights[0][1][2] == fun.TNT_RANGE[1] - 9
        assert any(c.startswith(f"summon minecraft:tnt {fun.TNT_RANGE[0] + 0.5}") for c in rcon)
        hand.clear()
        rcon.clear()
        fun_show = eng.fun
        fun_show._tnt_at = 0.0
        eng._pos["mika"] = (100.0, 70.0, 100.0)  # far away from the base
        eng._looking["mika"] = (100.0, 130.0, mm.time.time())
        await eng._heard("Mika", "[VR] blowUp")
        await real_sleep(0.02)  # it runs in the background
        assert hand and hand[0][1].endswith("minecraft:tnt")  # placed by her hand
        assert any(c.startswith("summon minecraft:tnt 100.5 70 108.5") for c in rcon)  # 8 ahead of her, lit
        await eng._heard("Mika", "[VR] blowUp")  # not again right away
        await real_sleep(0.02)
        assert sum(1 for c in rcon if c.startswith("summon minecraft:tnt")) == 1
        assert fun.wants_tnt("Mika use TNT!") and fun.wants_tnt("blow it up luna")

    asyncio.run(run())


def test_answers_know_what_they_can_really_do(monkeypatch):
    eng = _live_engine(monkeypatch)
    system, _user = eng._reply_prompt("mika", "Viewer", "can you dig?", [])
    assert "dig holes" in system and "TNT" in system and "cannot fight" in system
    assert "Never pretend" in system



def test_a_flower_garden_is_a_real_garden(monkeypatch):
    design = fb.parse_design(json.dumps({"title": "Flower Garden", "template": "garden", "size": "medium",
                                         "main": "stone_bricks", "accent": "gravel", "sky": False, "text": None}))
    blocks = fb.blocks_of(design)
    kinds = {b for _p, b in blocks}
    assert len(blocks) > 300  # not three blocks
    assert {"grass_block", "water", "lantern", "oak_fence"} <= kinds
    assert len(kinds & {f for fl in fb_templates().FLOWERS.values() for f in fl}) >= 5  # many kinds of flowers
    # bottom up: the lawn goes in before the flowers that stand on it
    order = [p for p, b in blocks]
    assert order.index((0, 0, 0)) < min(i for i, (p, b) in enumerate(blocks) if b in ("poppy", "cornflower", "pink_tulip",
                                                                                         "dandelion", "allium"))


def fb_templates():
    from src.open_llm_vtuber.room import minecraft_templates

    return minecraft_templates


def test_a_sky_castle_floats_with_the_name_on_it():
    design = fb.parse_design(json.dumps({"title": "Sky castle", "template": "castle", "size": "medium",
                                         "main": "quartz_block", "accent": "stone_bricks", "sky": True,
                                         "text": {"words": "Selwyn", "block": "gold_block"}}))
    blocks = fb.blocks_of(design)
    assert min(p[1] for p, _b in blocks) >= 8  # floating
    assert sum(1 for _p, b in blocks if b == "gold_block") > 30  # SELWYN in gold
    assert len(blocks) <= fb.MAX_TEMPLATE_BLOCKS


def test_a_giant_dragon_is_a_dragon():
    design = fb.parse_design(json.dumps({"title": "Giant dragon", "template": "dragon", "size": "large",
                                         "main": "red_concrete", "accent": "orange_terracotta", "sky": False}))
    blocks = fb.blocks_of(design)
    kinds = {b for _p, b in blocks}
    assert 1500 < len(blocks) <= fb.MAX_TEMPLATE_BLOCKS
    assert {"red_concrete", "orange_terracotta", "gold_block", "quartz_block", "red_wool"} <= kinds  # eyes, horns, wings
    assert min(p[1] for p, _b in blocks) == 0  # it stands on the ground
    xs = [p[0] for p, _b in blocks]
    assert max(xs) - min(xs) > 35  # the wings spread wide
    assert fb.parse_design('{"template": "dragon"}')["template"] == "dragon"


def test_they_roast_chat_unless_told_not_to(monkeypatch):
    eng = _live_engine(monkeypatch)
    monkeypatch.delenv("VR_ROAST", raising=False)
    system, _u = eng._reply_prompt("mika", "MathUnlockedYT", "where is my castle", [])
    assert "ROAST" not in system and "NEW here" in system  # a newcomer is welcomed, not roasted
    eng._answered_before.add("MathUnlockedYT")
    system, _u = eng._reply_prompt("mika", "MathUnlockedYT", "where is my castle", [])
    assert "ROAST the viewers" in system and "never cruel" in system
    assert "ROAST the viewers" in eng.profile("mika")["conversing"]
    monkeypatch.setenv("VR_ROAST", "0")
    system, _u = eng._reply_prompt("mika", "MathUnlockedYT", "where is my castle", [])
    assert "ROAST" not in system


def test_the_kingdom_is_built_lot_by_lot_and_chat_comes_first(monkeypatch):
    from src.open_llm_vtuber.room import minecraft_kingdom as kd

    async def run():
        eng = _live_engine(monkeypatch)
        eng.projects.state.update({"base": [0, 64, 0], "kingdom": {"lot": 0, "piece": 0}})
        now = mm.time.time()
        eng._seen_at = {"mika": now, "luna": now}
        eng._pos = {"mika": (0.5, 64.0, 0.5), "luna": (3.5, 64.0, 0.5)}
        sets, placed = [], []

        async def fake_rcon(cmd, reply=False):
            if cmd.startswith("setblock "):
                sets.append(cmd)
            if cmd.startswith("execute unless block"):
                return "Test passed" if int(cmd.split()[4]) <= 63 else "Test failed"
            return "ok"

        async def command(cid, text):
            if text.startswith("!layBlocks("):
                job = int(text[len("!layBlocks("):].split(",")[0])
                for i in range(len(text.rsplit('"', 2)[-2].split(";"))):
                    await eng._hand_event(["put", str(job), str(i)])
                await eng._hand_event(["laid", str(job)])
            return True

        async def place_one(cmd):
            placed.append(cmd)
            return True

        async def arrive(*a):
            return None

        async def no_wait(_s):
            return None

        monkeypatch.setattr(mm, "rcon_command", fake_rcon)
        monkeypatch.setattr(mm.asyncio, "sleep", no_wait)
        eng._command, eng._arrive, eng.projects.place_one = command, arrive, place_one
        # chat asked for something: the Kingdom waits right away, nothing lost
        eng._free_waiting = 1
        assert await eng._kingdom_step()
        assert eng.projects.state["kingdom"]["piece"] == 0 and not sets
        eng._free_waiting = 0
        assert await eng._kingdom_step()  # the whole first lot: the king's castle
        first = kd.lots()[0]
        assert {k: eng.projects.state["kingdom"][k] for k in ("lot", "piece")} == {"lot": 1, "piece": 0}
        assert len(sets) == len(set(sets)) == len(kd.lot_blocks(first)) and not placed  # all by hand
        assert first["title"] == "the king's castle" and "Kingdom" in eng._memory_text()
        assert any(op.get("title") == "🏰 The Kingdom" for op in eng.pushed)

    asyncio.run(run())


def test_the_kingdom_fills_a_long_stream():
    from src.open_llm_vtuber.room import minecraft_kingdom as kd

    lots = kd.lots()
    assert len(lots) == kd.GRID * kd.GRID and lots[0]["template"] == "castle"
    assert kd.total_blocks() > 150_000  # about 10 hours by hand, two girls
    assert len({(lot["x"], lot["z"]) for lot in lots}) == len(lots)  # every lot its own place
