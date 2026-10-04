"""The fixes for a 16 hour Minecraft stream: chat that keeps working, actions
that only fire on real requests, safe content, builds that never land on each
other, loops that restart themselves, a goodbye viewers really hear."""

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from src.open_llm_vtuber.live import youtube_live as yl
from src.open_llm_vtuber.room import minecraft_asks as asks
from src.open_llm_vtuber.room import minecraft_kingdom as kd
from src.open_llm_vtuber.room import minecraft_mode as mm
from src.open_llm_vtuber.room import minecraft_projects as mp
from src.open_llm_vtuber.room import minecraft_safety as safety


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(mm, "VIEWERS_FILE", tmp_path / "viewers.json")
    monkeypatch.setattr(mp, "STATE_FILE", tmp_path / "project.json")
    monkeypatch.setenv("VR_MODERATION", "0")


def _engine():
    profiles = {
        "mika": NS(name="Mika", persona="Bubbly witch.", reactions={}, emotions={}),
        "luna": NS(name="Luna", persona="Calm.", reactions={}, emotions={}),
    }
    session = NS(room=NS(characters=[NS(id="mika"), NS(id="luna")], get=profiles.get))
    eng = mm.MinecraftEngine(None, session)

    class Link:
        def __init__(self):
            self.sent = []
            self.connected = asyncio.Event()
            self.connected.set()

        async def emit(self, event, *args):
            self.sent.append(args)
            return True

    eng.link = Link()
    eng.pushed = []

    async def push(op):
        eng.pushed.append(op)

    eng._push = push
    eng._kick_answers = lambda: None
    return eng


def _message(i, text, author="viewer"):
    return yl.YouTubeChatMessage(f"id{i}", f"ch-{author}", author, text, datetime.now(timezone.utc))


# ---------------------------------------------------------------- chat


def test_the_same_comment_works_again_later():
    """After two people typed "race", nobody could for the rest of the stream."""
    buf = yl.YouTubeMessageBuffer(50, 120, 0)
    for i in range(3):
        ok, _why = buf.add(_message(i, "race", f"v{i}"))
        assert ok
        buf.mark_answered(buf.messages[-1])
    assert buf.add(_message(9, "race", "v9")) == (False, "repeated_spam")  # a flood right now is spam
    buf._recent_texts = type(buf._recent_texts)((t - 31, n) for t, n in buf._recent_texts)  # 31 s later
    ok, _why = buf.add(_message(10, "race", "v10"))
    assert ok


def test_the_channel_owner_is_heard_but_not_our_own_posts(monkeypatch):
    monkeypatch.delenv("VR_IGNORE_OWNER", raising=False)
    mine = yl.YouTubeChatMessage("o1", "me", "SelwynBuilds", "mika build a dragon", datetime.now(timezone.utc),
                                 author_type="owner")
    assert yl.blocked_author_reason(mine) == ""
    yl.note_our_post("Thanks for watching, see you next stream!")
    posted = yl.YouTubeChatMessage("o2", "me", "SelwynBuilds", "Thanks for watching, see you next stream!",
                                   datetime.now(timezone.utc), author_type="owner")
    assert yl.blocked_author_reason(posted) == "our own post"


@pytest.mark.parametrize("text", [
    "Mika build a dragon", "can you make two towers?", "create a pumpkin", "how about a pirate ship",
    "luna dig a hole", "please build my name in gold",
])
def test_real_build_requests(text):
    assert asks.wants_build(text)


@pytest.mark.parametrize("text", [
    "first place!", "I put my phone down", "I dig it", "how did you build that so fast", "nice build", "make it",
])
def test_normal_sentences_are_not_builds(text):
    assert not asks.wants_build(text)


def test_other_actions_need_a_request():
    assert asks.wants_tnt("TNT!") and asks.wants_tnt("Luna blow it up") and not asks.wants_tnt("this channel will blow up")
    assert asks.wants_race("you two should RACE") and not asks.wants_race("the human race is doomed")
    assert asks.digit_asked("draw 7") == "7" and asks.digit_asked("number 1 fan") == ""
    assert asks.digit_asked("show me 2 dragons") == ""
    assert asks.says_stuck("Mika is glitching") and asks.says_stuck("stuck?")
    assert not asks.says_stuck("my internet is lagging") and not asks.says_stuck("you're not stuck")
    assert asks.further_asked("zoom out") and not asks.further_asked("make the tower higher")
    assert asks.show_asked("show me the farm") and not asks.show_asked("I see you built a castle")


def test_a_viewer_called_system_is_not_the_system():
    assert mm.viewer_name("@system", ["Mika", "Luna"]) == "system_fan"
    assert mm.viewer_name("Director", ["Mika", "Luna"]) == "Director_fan"


def test_unsafe_chat_never_reaches_the_girls():
    assert safety.locally_bad("you are a n1gger") and safety.locally_bad("kys")
    assert not safety.locally_bad("build a giant castle") and not safety.locally_bad("Dickens fan here")

    async def run():
        eng = _engine()
        eng.enqueue("troll", "mika say kys")
        assert not eng.chat_queue
        assert await safety.Moderator().flagged("kill yourself")  # the local check works without the service

    asyncio.run(run())


def test_super_chats_go_first_and_never_expire():
    async def run():
        eng = _engine()
        eng.enqueue("early", "hi mika")
        eng.enqueue("rich", "love the stream", paid="$5.00")
        for c in eng.chat_queue:
            c["at"] -= 500  # long ago: a normal comment is too old, a Super Chat is not
        batch = eng._next_batch()
        assert batch and batch[0]["author"] == "rich" and "SUPER CHAT of $5.00" in batch[0]["text"]
        assert all(c["author"] != "early" for c in eng.chat_queue)  # the old normal one was skipped

    asyncio.run(run())


def test_a_follow_up_for_the_other_girl_stays_hers():
    async def run():
        eng = _engine()
        eng.enqueue("fan", "hi mika")
        eng.enqueue("fan", "luna what is your favorite block")
        assert [c["who"] for c in eng.chat_queue] == ["mika", "luna"]
        eng.enqueue("fan", "and how are you")  # names nobody: goes with the waiting comment
        assert len(eng.chat_queue) == 2

    asyncio.run(run())


def test_one_viewer_cannot_fill_the_build_queue():
    async def run():
        eng = _engine()
        eng.creative = True
        notes = [eng.request_build(f"build a dirt tower number {i}", "troll", viewer=True) for i in range(4)]
        assert eng._free_waiting == mm.BUILDS_PER_VIEWER
        assert "already has" in notes[-1]
        assert "already" in eng.request_build("build a dirt tower number 0", "someone", viewer=True)  # a duplicate
        for task in list(mm._BACKGROUND):
            task.cancel()

    asyncio.run(run())


def test_when_the_engine_acts_her_bot_is_not_asked_to_do_it_again(monkeypatch):
    async def run():
        eng = _engine()
        eng.creative = True

        async def no_reply(*a, **k):
            return "On it!"

        eng._quick_reply = no_reply
        eng.fun.drink = lambda *a, **k: asyncio.sleep(0)
        eng.enqueue("fan", "mika drink a potion of speed")
        eng.enqueue("other", "luna what is your favorite color")
        while eng.chat_queue:
            await eng._answer_loop()
        texts = [o["text"] for o in eng.outbox]
        assert len(texts) == 1 and "favorite color" in texts[0] and "reply with just: ok" in texts[0]

    asyncio.run(run())


# ---------------------------------------------------------------- building


def test_kingdom_lots_never_land_on_the_base_projects():
    eng = _engine()
    eng.projects.state.update({"base": [0, 64, 0], "plots": 19})
    p1, q1, p2, q2 = mp.LOADED  # the castle, network, farm and garden
    blocked, on_base = [], []
    for lot in kd.lots():
        spots = kd.lot_blocks(lot)
        xs = [x for x, _, _ in spots]
        zs = [z for _, _, z in spots]
        dz = -min(min(zs), 0)
        box = (lot["x"] + min(xs), lot["z"] + min(zs) + dz, lot["x"] + max(xs), lot["z"] + max(zs) + dz)
        if eng._taken(*box, pad=1):
            blocked.append(lot["n"])
        if box[0] <= p2 and box[2] >= p1 and box[1] <= q2 and box[3] >= q1:
            on_base.append(lot["n"])
    assert on_base and set(on_base) <= set(blocked)  # every lot on the base projects stays empty
    assert 0 not in blocked  # the king's castle (already started) is not
    assert len(blocked) < 40  # (the old viewer plots keep their ground too)


def test_the_kingdom_goes_lot_to_lot_and_round_two_is_elsewhere():
    lots = kd.lots()
    hops = [((a["x"] - b["x"]) ** 2 + (a["z"] - b["z"]) ** 2) ** 0.5 for a, b in zip(lots, lots[1:])]
    assert max(hops) < 100  # neighbours, not across the whole kingdom
    assert lots[0]["title"] == "the king's castle" and (lots[0]["x"], lots[0]["z"]) == (-45, -205)  # lot 0 as before
    second = kd.lots(1)
    assert second[0]["x"] == lots[0]["x"] - kd.ROUND_SHIFT


def test_viewer_plots_never_overlap():
    eng = _engine()
    eng.projects.state.update({"base": [0, 64, 0], "plots": 0})
    boxes = []
    for w, d in [(30, 40), (60, 80), (20, 20), (45, 45), (81, 81)] * 4:
        x, z = eng._plot_spot(w, d)
        boxes.append((x, z, x + w, z + d))
        assert x >= mm.PLOT_AREA_X
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            assert a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1]


def test_tnt_is_never_near_the_kingdom():
    eng = _engine()
    eng.projects.state.update({"base": [0, 64, 0], "kingdom": {"lot": 3, "piece": 0}})
    lot = kd.lots()[2]
    assert eng.near_builds(lot["x"], lot["z"])
    assert not eng.near_builds(-200, 320)  # the blast range


def test_project_progress_survives_a_crash_mid_save(tmp_path):
    book = mp.ProjectTracker(["Mika", "Luna"], None, None, None)
    book.state.update({"base": [1, 64, 2], "kingdom": {"lot": 7}})
    book._save()
    book._save()  # the .bak is the previous good copy
    mp.STATE_FILE.write_text('{"base": [1, 64')  # a crash halfway through writing
    again = mp.ProjectTracker(["Mika", "Luna"], None, None, None)
    assert again.state["base"] == [1, 64, 2] and again.state["kingdom"]["lot"] == 7


# ---------------------------------------------------------------- staying up


def test_a_failing_loop_starts_again():
    async def run():
        eng = _engine()
        calls = []

        real = asyncio.sleep
        forever = asyncio.Event()

        async def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise RuntimeError("boom")
            await forever.wait()

        async def fast(seconds, *a, **k):
            await real(0)

        mm.asyncio.sleep, saved = fast, mm.asyncio.sleep
        try:
            task = eng._supervised("test", flaky)
            for _ in range(20):
                await real(0)
            assert len(calls) == 3 and not task.done()
            task.cancel()
        finally:
            mm.asyncio.sleep = saved

    asyncio.run(run())


def test_the_goodbye_goes_first():
    eng = _engine()
    eng.chat_queue.append({"who": "mika", "author": "a", "text": "hi", "at": 0, "first": True})
    eng.lines.append({"who": "mika", "text": "chatter", "at": 0})
    eng.hush()
    assert not eng.chat_queue and not eng.lines
    eng.enqueue("late", "bye mika")
    assert not eng.chat_queue  # nothing new is answered over the goodbye


def test_big_logs_are_moved_aside(tmp_path, monkeypatch):
    log = tmp_path / "server.log"
    log.write_text("x" * 20)
    monkeypatch.setattr(mm, "LOG_KEEP_BYTES", 10)
    mm._rotate_log(log)
    assert not log.exists() and (tmp_path / "server.log.old").exists()


def test_new_members_and_stickers_are_read():
    js = (mm.Path(yl.__file__).parent / "youtube_chat_observer.js").read_text()
    assert "YT-LIVE-CHAT-MEMBERSHIP-ITEM-RENDERER" in js and "YT-LIVE-CHAT-PAID-STICKER-RENDERER" in js


def test_eyes_do_not_announce_the_friend_or_the_camera(monkeypatch):
    async def run():
        monkeypatch.setenv("VR_MINECRAFT_PLAYER", "SelwynBuilds")
        eng = _engine()
        eng.camera_player = "SelwynBuilds"
        seen = {"players": [{"name": "Luna", "d": 4, "where": "to the left"},
                            {"name": "SelwynBuilds", "d": 1, "where": "behind"}], "near": []}
        await eng.eyes.saw("mika", json.dumps(seen))
        assert not eng.link.sent  # nothing "new" to react to
        assert "SelwynBuilds" not in eng.eyes.describe_for("mika")

    asyncio.run(run())


def test_review_fixes():
    # everyday chat is not a build
    for text in ("make sure to subscribe", "put your hands up", "add me on discord", "make a wish", "place your bets"):
        assert not asks.wants_build(text), text
    # the stuck check needs whole words and the girls or the picture
    assert not asks.says_stuck("the submit button is broken") and not asks.says_stuck("my game keeps lagging")
    # an old kingdom keeps its lot order, a new one goes ring by ring
    assert mm.MinecraftEngine._kingdom_lots({"order": "old"}) != mm.MinecraftEngine._kingdom_lots({})
    assert mm.MinecraftEngine._kingdom_lots({"order": "old"})[0]["title"] == "the king's castle"


def test_a_lopsided_viewer_build_stays_on_its_plot():
    """Facing south a design's x is mirrored: the plot, its loading and its
    clearing used the unmirrored range, so lopsided builds landed beside it."""
    from src.open_llm_vtuber.room import minecraft_freebuild as fb

    left, x1, x2 = 500, 0, 20
    gx = left + x2 + 1  # as in _free_build_now
    xs = [gx + fb.to_world((x, 0, 0), (0, 1))[0] for x in range(x1, x2 + 1)]
    assert min(xs) == left + 1 and max(xs) == left + 1 + (x2 - x1)


def test_a_member_is_welcomed_not_thanked_for_money():
    async def run():
        eng = _engine()
        eng.enqueue("newfan", "Welcome to Selwyn Builds!", paid="a new membership")
        assert "MEMBER" in eng.chat_queue[-1]["text"] and "SUPER CHAT" not in eng.chat_queue[-1]["text"]

    asyncio.run(run())


def test_chat_can_change_the_build_going_on():
    """'It's too simple, make it more colorful, add gold on it! Build me a golden
    tower!' during the Finance Tower started a second build; now the tower changes."""
    eng = _engine()
    eng.creative = True
    steps = [{"commands": [f"fill {{x0}} {{y{i}}} {{z0}} {{x4}} {{y{i}}} {{z0}} minecraft:stone_bricks",
                           f"setblock {{x2}} {{y{i}}} {{z1}} minecraft:glass"]} for i in range(8)]
    eng._building = {"steps": steps, "next": 2, "title": "Finance Tower", "who": "FRCFinance"}
    text = "It's too simple can you make it more colorful? Like adding gold on it! Build me a golden tower!"
    eng.enqueue("FRCFinance", text)
    assert not eng._free_waiting and not eng._free_busy  # no second build
    assert "stone_bricks" in steps[1]["commands"][0]  # what is built already stays
    later = " ".join(c for st in steps[2:] for c in st["commands"])
    assert "stone_bricks" not in later and "_concrete" in later and "gold_block" in later
    assert "Finance Tower" in eng.chat_queue[-1]["text"]


def test_tour_friend_and_speed_are_understood():
    assert asks.tour_asked("are you building something? Mike fly around your whole world! I wanna see it")
    assert asks.friend_asked("Mike, where's luna? come to her and give her invisibility potion HAHA", "Luna")
    assert not asks.wants_build("Build faster mika don't stop until it's all done!")
    assert asks.wants_build("Can you build a tower?")
    assert mm.SPLASH_ASK.search("give her invisibility potion")


def test_the_network_panel_only_shows_after_a_viewer_digit():
    async def run():
        eng = _engine()
        await eng._net_push({"kind": "net", "steps": 3, "last": None})
        assert not eng.pushed  # training numbers alone: not on screen
        read = {"digit": 7, "guess": 7, "right": True}
        await eng._net_push({"kind": "net", "steps": 3, "last": read})
        await eng._net_push({"kind": "net", "steps": 4, "last": read})
        assert len(eng.pushed) == 1  # once, for the viewer's digit
        assert not any("draw 7" in h for h in mm.HINTS)

    asyncio.run(run())


def test_she_can_go_back_to_an_old_build():
    """'Mika can you look over the first tower you built?' - she said she was
    flying over and never moved."""
    async def run():
        eng = _engine()
        eng.creative = True
        eng.projects.state.update({"base": [0, 64, 0], "builds": [
            {"title": "Finance Tower", "who": "FRCFinance", "frame": [[10, 20, -30], [10, 5, 5]], "done": True},
            {"title": "Faster Mika", "who": "FRCFinance", "frame": [[50, 20, -30], [50, 5, 5]], "done": True}]})
        shown = []

        async def show(build, who):
            shown.append(build["title"])

        eng._show_build = show
        eng.enqueue("FRCFinance", "Mika can you look over the first tower you built? We are sharing the vision "
                                  "and I want to see it!")
        await asyncio.sleep(0)
        assert shown == ["Finance Tower"] and "flying back to Finance Tower" in eng.chat_queue[-1]["text"]

    asyncio.run(run())


def test_no_stage_directions_are_read_out_loud():
    assert mm.clean_reply("Here I go! *flies over dramatically* Wheee") == "Here I go! Wheee"


def test_a_girls_material_pick_changes_the_build_going_on():
    async def run():
        eng = _engine()
        steps = [{"commands": ["fill {x0} {y0} {z0} {x3} {y0} {z0} minecraft:stone_bricks"]} for _ in range(3)]
        eng._building = {"steps": steps, "next": 0, "title": "Finance Tower", "who": "FRCFinance"}
        await eng._heard("Luna", "[VR] changeMaterial gold_block")
        assert all("gold_block" in st["commands"][0] for st in steps)
        assert "Finance Tower" in eng.link.sent[-1][1]["message"]

    asyncio.run(run())


def test_the_camera_girl_builds_from_further_back():
    async def run():
        eng = _engine()
        eng.projects.state.update({"base": [0, 64, 0]})
        flights = {}

        async def fly(cid, view, focus):
            flights.setdefault(cid, view)
            return 0.0

        async def arrive(*a):
            return None

        async def hop(cid, hover, look, last):
            return 0.0

        async def lay(cid, run, hover=None, look=None):
            return True

        eng._fly, eng._arrive, eng._hop, eng._lay_by_hand = fly, arrive, hop, lay
        step = {"focus": (0, 0, 0), "view": (0, 0, -10), "commands": ["setblock {x0} {y0} {z0} minecraft:stone"]}
        await eng._lay_runs("mika", ["setblock {x0} {y0} {z0} minecraft:stone"] * 2, step)
        await eng._lay_runs("luna", ["setblock {x0} {y0} {z0} minecraft:stone"] * 2, step)
        assert flights["mika"][2] < flights["luna"][2]  # Mika (the stream) further back

    asyncio.run(run())


def test_can_i_see_the_network_and_visit_it():
    """'Can I see the neural network?' then 'Mika let's visit it now!': Luna
    started building a second network, and nobody flew anywhere."""
    async def run():
        eng = _engine()
        eng.creative = True
        eng.projects.state.update({"base": [0, 64, 0]})
        shown = []

        async def show(place, who):
            shown.append(place)

        eng._show_place = show
        eng.enqueue("FRCFinance", "Can I see the neural network?")
        await asyncio.sleep(0)
        assert shown == ["network"]
        eng._camera_moved_at = 0.0
        eng.enqueue("FRCFinance", "Mika let's visit it now!")
        await asyncio.sleep(0)
        assert shown == ["network", "network"] and "flying to the network" in eng.chat_queue[-1]["text"]
        await eng._heard("Luna", "[VR] buildThis neural network")  # her bot wanted to build it
        await asyncio.sleep(0)
        assert not eng._free_waiting and shown[-1] == "network"
        await eng._heard("Luna", "[VR] buildThis a sky castle")  # a new idea is still built
        assert eng._free_waiting == 1
        for task in list(mm._BACKGROUND):
            task.cancel()

    asyncio.run(run())


def test_viewer_builds_go_on_kingdom_lots_close_by():
    """The far plot area (420 blocks away, never loaded) showed black holes on
    stream. A viewer build now takes the next free Kingdom lot(s)."""
    from src.open_llm_vtuber.room import minecraft_kingdom as kd

    eng = _engine()
    eng.projects.state.update({"base": [0, 64, 0], "kingdom": {"lot": 0, "piece": 4}})
    lots = kd.lots()
    small = eng._claim_lots(20, 30)
    big = eng._claim_lots(70, 70)
    for x, z in (small, big):
        near = min(abs(lot["x"] - x) + abs(lot["z"] - z) for lot in lots[:30])
        assert near < 100  # among the first lots, next to where they work
    claimed = {tuple(c) for c in eng.projects.state["kingdom"]["claimed"]}
    assert len(claimed) == 1 + 4  # one lot, then a 2 x 2 block
    # the Kingdom skips them
    assert any(eng._lot_cell(lot) in claimed for lot in lots[1:30])


def test_golems_come_alive_but_never_the_wither():
    """Chat wanted golems (a carved pumpkin on snow blocks); a Wither would wreck the builds."""
    from src.open_llm_vtuber.room import minecraft_freebuild as fb

    assert fb.block_id("carved_pumpkin") == "carved_pumpkin"
    assert fb.block_id("wither_skeleton_skull") == "skeleton_skull"


def test_build_it_where_you_stand_and_another_one():
    async def run():
        eng = _engine()
        eng.creative = True
        eng.projects.state.update({"base": [0, 64, 0], "kingdom": {"lot": 0, "piece": 0}})
        eng._pos["mika"] = (300.0, 70.0, 300.0)
        x, z = eng._spot_here(20, 20)
        assert abs(x + 10 - 300) < 3 and 300 < z < 320  # just ahead of her
        asked = []
        eng.request_build = lambda request, who, viewer=False, here=False: asked.append((request, here)) or ""
        eng.enqueue("FRCFinance", "Let's build many 10 snowmen!")
        eng.enqueue("FRCFinance", "Build another 10 where you stand right now")
        assert asked[-1][1] is True and "10 snowmen" in asked[-1][0]

    asyncio.run(run())


def test_she_turns_to_what_she_sees(monkeypatch):
    """'Look, snow golems!' - and the stream (her eyes) shows them."""
    async def run():
        eng = _engine()
        eng._pos["mika"] = (10.0, 70.0, 10.0)
        asked, orders = [], []

        async def rcon(cmd, reply=False):
            asked.append(cmd)
            return "Snow Golem has the following entity data: [20.5d, 69.0d, 15.5d]"

        async def command(cid, text):
            orders.append((cid, text))
            return True

        monkeypatch.setattr(mm, "rcon_command", rcon)
        eng._command = command
        seen = {"near": [{"name": "snow_golem", "d": 9, "where": "to the left"}], "players": []}
        await eng.eyes.saw("mika", json.dumps(seen))
        assert "@e[type=minecraft:snow_golem" in asked[-1]
        assert orders[-1] == ("mika", "!flyTo(10.0, 70.0, 10.0, 20.5, 70.0, 15.5, 0)")  # only her head turns
        assert eng._chose_at["mika"] + mm.CHOICE_HOLD > mm.time.time() + 5  # the build waits while she looks
        assert "React" in eng.link.sent[-1][1]["message"]

    asyncio.run(run())


def test_spells_are_understood():
    from src.open_llm_vtuber.room import minecraft_spells as sp

    assert sp.spells_asked("Mika cast lightning on Luna") == ["lightning"]
    assert sp.spells_asked("make Luna giant") == ["giant"]
    assert sp.spells_asked("summon a parrot!") == ["summon"]
    assert sp.spells_asked("mix levitation and fireworks together, cast it!") == ["levitate", "fireworks"]
    assert sp.spells_asked("let's experiment on spells today! freeze her") == ["freeze"]
    assert sp.spells_asked("I love rain") == [] and sp.spells_asked("big tower please") == []
    assert sp.creature_asked("summon a puppy") == "wolf" and sp.creature_asked("spawn an iron golem") == "iron_golem"
    names = {"mika": "Mika", "luna": "Luna"}
    assert sp.target_asked("cast lightning on Luna", names, "mika") == "luna"
    assert sp.target_asked("freeze the cow", names, "mika") == "mob"


def test_a_spell_is_cast_with_a_wave_and_really_happens(monkeypatch):
    async def run():
        eng = _engine()
        eng.creative = True
        eng._pos = {"mika": (0.0, 70.0, 0.0), "luna": (5.0, 70.0, 0.0)}
        sent, orders = [], []

        async def rcon(cmd, reply=False):
            sent.append(cmd)
            return ""

        async def command(cid, text):
            orders.append((cid, text))
            return True

        monkeypatch.setattr(mm, "rcon_command", rcon)
        eng._command = command
        eng.spells._cast_at = 0.0
        result = await eng.spells.cast("mika", ["lightning", "fireworks"], "luna", "fan")
        assert orders[0][0] == "mika" and orders[0][1].startswith("!gesture(5.0")  # she turns to Luna and waves
        assert "gamerule doFireTick false" in sent  # lightning never burns a build
        assert any(c.startswith("summon minecraft:lightning_bolt") for c in sent)
        assert sum(c.startswith("summon minecraft:firework_rocket") for c in sent) == 5
        assert "lightning" in result and "mixed" in result
        sent.clear()
        eng.spells._cast_at = 0.0
        await eng.spells.cast("luna", ["giant"], "mika", "fan")  # never on the camera girl
        assert not any("minecraft:scale" in c for c in sent)
        for task in list(mm._BACKGROUND):
            task.cancel()

    asyncio.run(run())


def test_a_splash_potion_is_really_thrown(monkeypatch):
    async def run():
        eng = _engine()
        eng._pos = {"mika": (0.0, 70.0, 0.0), "luna": (4.0, 70.0, 0.0)}
        sent, orders = [], []

        async def rcon(cmd, reply=False):
            sent.append(cmd)
            return ""

        async def command(cid, text):
            orders.append(text)
            return True

        async def fast(*a, **k):
            return None

        monkeypatch.setattr(mm, "rcon_command", rcon)
        monkeypatch.setattr(eng.fun, "rcon", lambda cmd, reply=False: rcon(cmd, reply))
        from src.open_llm_vtuber.room import minecraft_fun as fun

        monkeypatch.setattr(fun.asyncio, "sleep", fast)
        eng._command = command
        await eng.fun.splash("mika", "glowing")
        assert any("splash_potion" in c and "minecraft:glowing" in c for c in sent)
        assert orders and orders[0].startswith("!gesture(4.0") and orders[0].endswith(", 1)")  # thrown at Luna

    asyncio.run(run())


def test_the_girls_know_their_spells_and_golems():
    assert "!castSpell" in mm.CAN_DO and "iron golem" in mm.CAN_DO and "lightning" in mm.CAN_DO
    assert "!castSpell" in mm.FLY_COMMANDS and "!gesture" in mm.FLY_COMMANDS


def test_magic_is_not_a_build():
    from src.open_llm_vtuber.room import minecraft_spells as sp

    async def run():
        eng = _engine()
        eng.creative = True
        cast = []

        async def fake_cast(caster, spells, target="friend", who="", creature="", girl=False):
            cast.append((caster, spells, target))
            return "done"

        eng.spells.cast = fake_cast
        eng.enqueue("fan", "make Luna giant")
        await asyncio.sleep(0)
        assert cast == [("mika", ["giant"], "luna")] and not eng._free_waiting
        assert sp.spells_asked("make the tower bigger") == [] and sp.spells_asked("build a giant tower") == []

    asyncio.run(run())


def test_splash_that_chicken_and_place_gold_there():
    from src.open_llm_vtuber.room import minecraft_fun as fun

    assert fun.potion_ask("splash something on that chicken") in fun.MYSTERY
    assert asks.creature_named("splash something on that chicken") == "chicken"
    assert asks.place_there_asked("place some in that block over there, use gold") == ("gold_block", 3)
    assert asks.place_there_asked("put 5 diamond blocks where you're looking") == ("diamond_block", 5)
    assert asks.place_there_asked("put your hands up") == ("", 0)

    async def run():
        eng = _engine()
        eng.creative = True
        eng.projects.state.update({"base": [0, 64, 0]})
        eng._pos["mika"] = (0.0, 70.0, 0.0)
        splashed, laid = [], []

        async def splash(cid, effect, at=""):
            splashed.append((cid, at))

        async def look(cid, reason=""):
            eng.eyes.looking_at[cid] = (5, 64, 7, "grass_block", mm.time.time())
            return ""

        async def lay(cid, run, hover=None, look=None):
            laid.append((cid, run))
            return True

        eng.fun.splash, eng.eyes.look, eng._lay_by_hand = splash, look, lay
        eng.enqueue("fan", "Mika splash something on that chicken")
        eng.enqueue("fan2", "place some in that block over there, use gold")
        for _ in range(3):
            await asyncio.sleep(0)
        assert splashed == [("mika", "chicken")]
        assert laid == [("mika", "fill {x5} {y1} {z7} {x5} {y3} {z7} minecraft:gold_block")]  # 3 on top of it
        assert not eng._free_waiting  # not a build, not a restyle

    asyncio.run(run())
