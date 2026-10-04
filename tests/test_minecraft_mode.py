from types import SimpleNamespace as NS

import pytest

from src.open_llm_vtuber.room import minecraft_mode as mm


@pytest.fixture(autouse=True)
def _own_viewer_file(tmp_path, monkeypatch):
    """The viewer memory of a test never touches the real one."""
    monkeypatch.setattr(mm, "VIEWERS_FILE", tmp_path / "viewers.json")


def test_clean_line_cuts_commands_and_notices():
    assert mm.clean_line('(To Luna) Look, diamonds!! !collectBlocks("diamond_ore", 3)') == ("Look, diamonds!!", "Luna")
    assert mm.clean_line('On it! !goal("Build a (big) house")') == ("On it!", "")
    assert mm.clean_line("!stop")[0] == ""
    assert mm.clean_line("My brain disconnected, try again.")[0] == ""
    assert mm.clean_line("[LoginGuard] Connection Error: x")[0] == ""
    assert mm.clean_line("*picks up dirt* Got some dirt!")[0] == "Got some dirt!"
    assert mm.clean_line("My error-free wall!")[0] == "My error-free wall!"


def test_viewers_cannot_run_commands_or_pose_as_bots():
    assert "!" not in mm.viewer_text('Mika !newAction("x") !stop pls')
    assert mm.viewer_name("@Luna", ["Mika", "Luna"]) == "Luna_fan"
    assert mm.viewer_name("Cool Viewer!", ["Mika"]) == "Cool Viewer"


def _engine():
    profiles = {
        "mika": NS(name="Mika", persona="Bubbly witch.", reactions={"happy": ("smile",)}, emotions={}),
        "luna": NS(name="Luna", persona="Calm.", reactions={}, emotions={"joy": "smile"}),
    }
    session = NS(room=NS(characters=[NS(id="mika"), NS(id="luna")], get=profiles.get))
    return mm.MinecraftEngine(None, session)


def test_targets_by_name_both_or_turns():
    eng = _engine()
    assert eng._targets("luna come here") == ["luna"]
    assert eng._targets("both of you dig") == ["mika", "luna"]
    first, second = eng._targets("hello"), eng._targets("hi")
    assert first != second


def test_profile_is_in_character():
    eng = _engine()
    profile = eng.profile("mika")
    assert profile["name"] == "Mika" and profile["model"]
    settings = eng.mindcraft_settings()
    assert settings["auth"] == "offline" and settings["render_bot_view"] is True
    assert settings["auto_open_ui"] is False and len(settings["profiles"]) == 2


def test_chunks_stay_short():
    parts = mm.chunks("One. " * 200)
    assert len(parts) <= 3 and all(len(p) <= mm.MAX_SAY for p in parts)


# ---------------------------------------------------------------- live fixes
import asyncio  # noqa: E402

from src.open_llm_vtuber.room.speech import playback_wait  # noqa: E402
from src.open_llm_vtuber.vr_agent.text_safety import strip_emoji  # noqa: E402


def test_no_emoji_is_ever_spoken():
    assert mm.clean_line("We did it!! 🎉🏰✨ Castle time 😂")[0] == "We did it!! Castle time"
    assert mm.clean_line("Love you ❤️ Luna :) <3 xD")[0] == "Love you Luna"
    assert mm.clean_line("🎉🎉")[0] == ""
    assert strip_emoji("family 👨‍👩‍👧 flag 🇵🇭 key 1️⃣ ⛏️") == "family flag key 1"
    assert strip_emoji("at 10:30, see http://x.y/a") == "at 10:30, see http://x.y/a"


def test_bot_messages_that_are_not_speech():
    for notice in (
        "Agent stopped. Self-prompting still active.",
        "Agent did not use command in the last 3 auto-prompts. Stopping auto-prompting.",
        "*Director used goal*",
    ):
        assert mm.clean_line(notice)[0] == "", notice


def test_speech_waits_only_as_long_as_the_clip():
    assert playback_wait(4000, 10) == 6.5  # 4 s clip: never the old 22 s
    assert playback_wait(0, 3) == 7.0


def test_profile_has_no_emoji_and_no_fighting_modes():
    eng = _engine()
    profile = eng.profile("luna")
    assert profile["modes"]["elbow_room"] is False and profile["modes"]["idle_staring"] is False
    assert "NEVER use emojis" in profile["conversing"]
    assert "moveAway(150)" not in profile["conversing"]


class FakeLink:
    def __init__(self):
        self.sent = []
        self.connected = asyncio.Event()
        self.connected.set()

    async def emit(self, event, *args):
        self.sent.append(args)
        return True


def _live_engine(monkeypatch):
    eng = _engine()
    eng.link = FakeLink()
    eng.pushed = []

    async def push(op):
        eng.pushed.append(op)

    eng._push = push
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return eng


def test_viewer_gets_a_spoken_answer_right_away(monkeypatch):
    async def run():
        eng = _live_engine(monkeypatch)
        eng.lines.extend({"who": "mika", "text": f"chatter {i}", "at": mm.time.time()} for i in range(4))
        eng.enqueue("MathUnlockedYT", "Luna hi 😊")
        await asyncio.sleep(0.05)
        assert eng.viewer_lines and eng.viewer_lines[0]["who"] == "luna"
        assert "MathUnlockedYT" in eng.viewer_lines[0]["text"]
        # the bot still gets it, told not to greet twice
        assert eng.outbox[0]["to"] == "luna" and "already answered" in eng.outbox[0]["text"]
        assert "😊" not in eng.outbox[0]["text"]
        # chatter that arrives afterwards cannot push the answer out
        for i in range(6):
            eng.lines.append({"who": "mika", "text": f"more {i}", "at": mm.time.time()})
        spoken = []

        async def say(cid, text, mood="", short=False):
            spoken.append((cid, text))

        eng._say = say
        task = asyncio.create_task(eng._speak_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        assert spoken[0][0] == "luna" and "MathUnlockedYT" in spoken[0][1]

    asyncio.run(run())


def test_answer_from_the_model_is_cleaned(monkeypatch):
    async def run():
        eng = _live_engine(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "test")

        class Done:
            choices = [NS(message=NS(content='"Hi Sam! 😊 On it! !collectBlocks("oak_log", 5) 🌳"'))]

        async def create(**kwargs):
            assert kwargs["max_tokens"] <= 80
            return Done()

        eng._llm = NS(chat=NS(completions=NS(create=create)))
        text = await eng.reply_text("mika", "Sam", "get wood")
        assert text == "Hi Sam! On it!"

    asyncio.run(run())


def test_unnamed_chat_goes_to_who_spoke_least():
    eng = _engine()
    eng._spoke_at = {"mika": 100.0, "luna": 50.0}
    assert eng._targets("hello there") == ["luna"]
    eng._spoke_at = {"mika": 50.0, "luna": 100.0}
    assert eng._targets("hello again") == ["mika"]


def _state(x, z, kind="acting"):
    return {"gameplay": {"position": {"x": x, "y": 64, "z": z}, "health": 20, "hunger": 20}, "action": {"kind": kind}}


def test_a_bot_without_a_goal_gets_one(monkeypatch):
    async def run():
        monkeypatch.setenv("VR_MINECRAFT_CREATIVE", "0")  # survival: the director walks them
        eng = _live_engine(monkeypatch)
        clock = [1000.0]
        monkeypatch.setattr(mm.time, "time", lambda: clock[0])
        eng._kind["mika"] = "stopped"
        eng._pos["mika"] = (0.0, 64.0, 0.0)
        await eng._direct("mika")
        name, msg = eng.link.sent[-1]
        assert name == "Mika" and msg["from"] == mm.DIRECTOR and msg["message"].startswith('!goal("')
        assert msg["message"].count('"') == 2  # one quoted argument, nothing that breaks it
        assert strip_emoji(msg["message"]) == msg["message"]  # no emoji for her to copy
        count = len(eng.link.sent)
        clock[0] += 5
        await eng._direct("mika")
        assert len(eng.link.sent) == count  # not again within GOAL_RETRY

    asyncio.run(run())


def test_a_stuck_bot_is_moved_out_and_her_view_reloaded(monkeypatch):
    async def run():
        monkeypatch.setenv("VR_MINECRAFT_CREATIVE", "0")  # survival: the director walks them
        eng = _live_engine(monkeypatch)
        clock = [1000.0]
        monkeypatch.setattr(mm.time, "time", lambda: clock[0])

        async def no_wait(_s):
            return None

        monkeypatch.setattr(mm.asyncio, "sleep", no_wait)
        commands = []

        async def rcon(cmd, reply=False):
            commands.append(cmd)
            return "Spread 1 player around 10, 20"

        monkeypatch.setattr(mm, "rcon_command", rcon)
        for cid in ("mika", "luna"):
            eng._kind[cid] = "acting"
            eng._goal_at[cid] = clock[0]
            eng._seen_at[cid] = clock[0]
        eng._goal_step = str(eng.projects.view().get("step") or "")
        eng._pos["luna"] = (10.0, 64.0, 20.0)
        eng._pos["mika"] = (0.0, 64.0, 0.0)
        await eng._direct("mika")
        clock[0] += 30
        eng._seen_at["luna"] = clock[0]
        eng._pos["mika"] = (1.0, 64.0, 1.0)  # moved under 3 blocks
        await eng._direct("mika")
        assert not commands
        clock[0] += 31  # 61 s in the same spot
        eng._seen_at["luna"] = clock[0]
        await eng._direct("mika")
        assert commands and commands[0].startswith("spreadplayers 10 20 ") and commands[0].endswith(" Mika")
        sent = [m["message"] for _n, m in eng.link.sent]
        assert "!stop" in sent and sent[-1].startswith("!goal(")
        assert {"kind": "reload", "who": "mika"} in eng.pushed

    asyncio.run(run())


def test_a_jump_reloads_the_view(monkeypatch):
    async def run():
        eng = _live_engine(monkeypatch)

        async def nothing(*a, **k):
            return None

        eng._keep_together = nothing
        eng.projects.update = nothing
        eng._direct = nothing
        await eng._state({"Mika": _state(0, 0)})
        assert {"kind": "reload", "who": "mika"} not in eng.pushed
        await eng._state({"Mika": _state(40, 0)})
        assert {"kind": "reload", "who": "mika"} in eng.pushed

    asyncio.run(run())


def test_real_game_camera_follows_the_speaker(monkeypatch):
    async def run():
        monkeypatch.setenv("VR_MINECRAFT_PLAYER", "Selwyn")
        monkeypatch.setenv("VR_MINECRAFT_CAMERA", "eyes")
        eng = _live_engine(monkeypatch)
        clock = [1000.0]
        monkeypatch.setattr(mm.time, "time", lambda: clock[0])
        commands = []
        online = {"mika", "luna"}

        async def rcon(cmd, reply=False):
            commands.append(cmd)
            return True

        async def who():
            return set(online)

        monkeypatch.setattr(mm, "rcon_command", rcon)
        monkeypatch.setattr(mm, "rcon_online", who)
        await eng._camera_tick()
        assert not eng.camera_on and not commands  # not in the world yet: web views
        online.add("selwyn")
        await eng._camera_tick()
        assert eng.camera_on and [c for c in commands if not c.startswith("kill")][:3] == [
            "gamemode creative Selwyn", "gamemode spectator Selwyn", "spectate Mika Selwyn"
        ]
        assert {"kind": "camera", "mode": "client"} in eng.pushed
        eng._seen_at["luna"] = clock[0]
        await eng._camera_to("luna")
        assert [c for c in commands if c.startswith("spectate")][-1] == "spectate Mika Selwyn"  # held
        clock[0] += mm.CAMERA_HOLD + 1
        eng._seen_at["luna"] = clock[0]
        await eng._camera_to("luna")
        assert [c for c in commands if c.startswith("spectate")][-1] == "spectate Luna Selwyn"
        assert {"kind": "focus", "who": "luna"} in eng.pushed
        online.discard("selwyn")
        await eng._camera_tick()
        assert not eng.camera_on and {"kind": "camera", "mode": "web"} in eng.pushed

    asyncio.run(run())


def test_neural_network_is_the_project_after_the_castle():
    from src.open_llm_vtuber.room import minecraft_projects as mp

    net = mp.PROJECTS[1]
    assert net["id"] == "neural_net" and len(net["milestones"]) == 5
    for _name, _gather, build in net["milestones"]:
        for command in build():
            placed = mp.place(command, (8, 62, -81), {})
            assert "{" not in placed


def test_creative_is_the_default_and_the_ai_only_talks(monkeypatch):
    monkeypatch.delenv("VR_MINECRAFT_CREATIVE", raising=False)
    eng = _engine()
    assert eng.creative and eng.projects.creative
    settings = eng.mindcraft_settings()
    assert settings["base_profile"] == "creative"
    blocked = settings["blocked_actions"]
    assert "!collectBlocks" in blocked and "!moveAway" in blocked
    for needed in ("!flyTo", "!land", "!stop", "!goal"):  # the engine sends these itself
        assert needed not in blocked
    profile = eng.profile("mika")
    assert profile["modes"]["unstuck"] is False and profile["modes"]["self_preservation"] is False
    assert "CREATIVE" in profile["conversing"] and "NEVER use emojis" in profile["conversing"]
    monkeypatch.setenv("VR_MINECRAFT_CREATIVE", "0")
    survival = _engine()
    assert not survival.creative and survival.mindcraft_settings()["base_profile"] == "survival"


def test_the_flight_patch_replaces_an_older_version(tmp_path, monkeypatch):
    actions = tmp_path / "src/agent/commands/actions.js"
    actions.parent.mkdir(parents=True)
    old = mm.FLY_MARK + " for building on stream (v1)\n        name: '!flyTo',\n    },\n"
    actions.write_text("const actionsList = [\n" + old + mm.FLY_ANCHOR + "\n    }\n];\n")
    monkeypatch.setattr(mm, "MINDCRAFT_DIR", tmp_path)
    mm.patch_mindcraft()
    text = actions.read_text()
    assert text.count(mm.FLY_MARK) == 1 and mm.FLY_COMMANDS in text and "(v1)" not in text
    mm.patch_mindcraft()
    assert actions.read_text() == text


def test_director_camera_frames_both_girls_and_the_build(monkeypatch):
    async def run():
        monkeypatch.setenv("VR_MINECRAFT_PLAYER", "Selwyn")
        monkeypatch.setenv("VR_MINECRAFT_CAMERA", "director")
        eng = _live_engine(monkeypatch)
        clock = [1000.0]
        monkeypatch.setattr(mm.time, "time", lambda: clock[0])
        commands = []

        async def rcon(cmd, reply=False):
            commands.append(cmd)
            return "Test passed" if cmd.startswith("execute if entity") else "ok"

        async def who():
            return {"mika", "luna", "selwyn"}

        monkeypatch.setattr(mm, "rcon_command", rcon)
        monkeypatch.setattr(mm, "rcon_online", who)
        for cid, pos in (("mika", (0.0, 70.0, 0.0)), ("luna", (0.0, 70.0, 6.0))):
            eng._pos[cid] = pos
            eng._seen_at[cid] = clock[0]
        eng.build_focus = (20.0, 72.0, 3.0)
        await eng._camera_tick()
        # your view rides an invisible camera stand: no teleports of the player itself
        assert not any(c.startswith("tp Selwyn") for c in commands)
        assert any(c.startswith("summon minecraft:armor_stand") and "Invisible:1b" in c for c in commands)
        assert commands[-1] == f"spectate @e[tag={mm.CAM_TAG},limit=1] Selwyn"
        x, y, z = eng._pose[:3]
        assert x < 0 and y > 70  # behind and above the girls, looking at them and the build
        # the next piece is on the other side: the stand glides there in small steps
        eng.build_focus = (-30.0, 72.0, 3.0)
        clock[0] += 1
        eng._seen_at = {"mika": clock[0], "luna": clock[0]}
        await eng._camera_tick()
        assert eng._shot[0][0] > 0
        start = eng._pose[0]
        steps = []
        real_sleep = asyncio.sleep

        async def tick(_s):
            await real_sleep(0)
            if len(steps) >= 40:
                raise asyncio.CancelledError
            steps.append(eng._pose[0])

        monkeypatch.setattr(mm.asyncio, "sleep", tick)
        try:
            await eng._camera_follow()
        except asyncio.CancelledError:
            pass
        moves = [c for c in commands if c.startswith(f"tp @e[tag={mm.CAM_TAG}")]
        assert len(moves) >= 20  # many tiny moves, not one jump
        deltas = [abs(b - a) for a, b in zip(steps, steps[1:])]
        assert max(deltas) < 3 and eng._pose[0] > start  # smooth, heading to the new side

    asyncio.run(run())


def test_the_girls_choose_what_happens(monkeypatch):
    async def run():
        eng = _live_engine(monkeypatch)
        eng.projects.state.update({"project": 0, "milestone": 0, "built": 0, "base": [8, 62, -81]})
        flights = []

        async def fly(cid, view, focus):
            flights.append((cid, view, focus))
            return 0.0

        async def arrive(*a):
            return None

        eng._fly = fly
        eng._arrive = arrive
        # her own chat line with the marker is an action, never speech
        await eng._heard("Mika", "[VR] buildNext")
        assert eng._build_now == "mika" and eng._build_event.is_set() and not eng.lines
        await eng._heard("Luna", "[VR] flyToPlace sky")
        await asyncio.sleep(0)
        assert flights[-1] == ("luna", *mm.PLACES["sky"]) and "luna" in eng._chose_at
        await eng._heard("Mika", "[VR] changeMaterial quartz_block")
        swapped = eng.projects._swap("fill {x-9} {y} {z-9} {x9} {y} {z9} minecraft:stone_bricks")
        assert swapped.endswith("minecraft:quartz_block")
        assert eng.projects._swap("setblock {x0} {y5} {z9} minecraft:stone_brick_wall").endswith("stone_brick_wall")
        await eng._heard("Mika", "[VR] changeMaterial tnt")  # not allowed: told, nothing changes
        assert eng.link.sent[-1][1]["message"].startswith("tnt is not on the list")
        # Nothing being built (training time / part done): a clear reason, not "cannot be used"
        eng.projects.timed = lambda: True
        await eng._heard("Mika", "[VR] changeMaterial birch_planks")
        assert eng.link.sent[-1][1]["message"].startswith("Nothing is being built right now")
        assert "Do not try other materials" in eng.link.sent[-1][1]["message"]
        profile = eng.profile("luna")["conversing"]
        assert "Selwyn Builds" in profile and "!flyToPlace" in profile

    asyncio.run(run())


def test_the_camera_is_mikas_eyes_by_default(monkeypatch):
    async def run():
        monkeypatch.setenv("VR_MINECRAFT_PLAYER", "Selwyn")
        monkeypatch.delenv("VR_MINECRAFT_CAMERA", raising=False)
        eng = _live_engine(monkeypatch)
        commands = []

        async def rcon(cmd, reply=False):
            commands.append(cmd)
            return "ok"

        async def who():
            return {"mika", "luna", "selwyn"}

        monkeypatch.setattr(mm, "rcon_command", rcon)
        monkeypatch.setattr(mm, "rcon_online", who)
        await eng._camera_tick()
        assert "spectate Mika Selwyn" in commands and not any("armor_stand" in c for c in commands)
        eng._seen_at["luna"] = mm.time.time()
        eng._cam_focus_at = 0
        await eng._camera_to("luna")  # Luna talks: the camera stays with Mika
        assert not any(c == "spectate Luna Selwyn" for c in commands)

    asyncio.run(run())


def test_both_girls_lay_a_piece_block_by_block(monkeypatch):
    """By hand: one swing per block, each block set on its swing, none twice."""
    async def run():
        eng = _live_engine(monkeypatch)
        eng.projects.state.update({"project": 0, "milestone": 0, "built": 0, "base": [8, 62, -81]})
        eng.projects.state.pop("material", None)  # no swap left over from another test
        hops, placed, rcon = [], [], []

        async def command(cid, text):
            hops.append((cid, text))
            if text.startswith("!layBlocks("):
                job = int(text[len("!layBlocks("):].split(",")[0])
                spots = text.rsplit('"', 2)[-2].split(";")
                for i in range(len(spots)):  # her hand hits each block in turn
                    await eng._hand_event(["put", str(job), str(i)])
                await eng._hand_event(["laid", str(job)])
            return True

        async def place_one(cmd):
            placed.append(cmd)
            return True

        async def fake_rcon(cmd, reply=False):
            rcon.append(cmd)
            return "ok"

        async def arrive(*a):
            return None

        async def no_wait(_s):
            return None

        eng._command = command
        eng._arrive = arrive
        eng.projects.place_one = place_one
        monkeypatch.setattr(mm, "rcon_command", fake_rcon)
        monkeypatch.setattr(mm.asyncio, "sleep", no_wait)
        step = {"commands": ["fill {x-9} {y1} {z-9} {x9} {y1} {z9} minecraft:stone_bricks outline"],
                "focus": (0, 1, 0), "view": (0, 8, -18)}
        runs = mm.lay(step["commands"][0])
        half = (len(runs) + 1) // 2
        await asyncio.gather(eng._lay_runs("mika", runs[:half], step), eng._lay_runs("luna", runs[half:], step))
        assert not placed  # nothing just appeared: all of it was laid by hand
        sets = [c for c in rcon if c.startswith("setblock ")]
        expected = sum(len(mm.hand_blocks(eng.projects.absolute(r))[0]) for r in runs)
        assert len(sets) == len(set(sets)) == expected and expected > 70  # every block once
        assert len([c for c in rcon if c.startswith("playsound minecraft:block.stone.place")]) == expected
        lays = [(c, t) for c, t in hops if t.startswith("!layBlocks(")]
        assert {c for c, _t in lays} == {"mika", "luna"} and len(lays) == len(runs)
        assert all('"stone_bricks"' in t for _c, t in lays)  # the block in her hand
        short = [t for _c, t in hops if t.endswith(", -60)")]
        assert len(short) == len(runs)
        assert not eng._jobs

    asyncio.run(run())


def test_an_interrupted_run_is_still_finished(monkeypatch):
    async def run():
        eng = _live_engine(monkeypatch)
        eng.projects.state.update({"project": 0, "milestone": 0, "built": 0, "base": [8, 62, -81]})
        placed, rcon = [], []

        async def command(cid, text):
            job = int(text[len("!layBlocks("):].split(",")[0])
            await eng._hand_event(["put", str(job), "0"])  # one block, then a new action interrupts her
            await eng._hand_event(["put", str(job), "0"])  # a repeat never sets it twice
            await eng._hand_event(["laid", str(job)])
            return True

        async def place_one(cmd):
            placed.append(cmd)
            return True

        async def fake_rcon(cmd, reply=False):
            rcon.append(cmd)
            return "ok"

        eng._command = command
        eng.projects.place_one = place_one
        monkeypatch.setattr(mm, "rcon_command", fake_rcon)
        run_cmd = "fill {x0} {y1} {z0} {x5} {y1} {z0} minecraft:oak_planks"
        assert await eng._lay_by_hand("mika", run_cmd)
        # she keeps laying the missed blocks herself, one per swing (3 tries)
        sets = [c for c in rcon if c.startswith("setblock")]
        assert len(sets) == len(set(sets)) == 3
        assert placed == [run_cmd]  # only what she never managed appears at the end
        # summons and clearing just happen, never by hand
        placed.clear()
        assert await eng._lay_by_hand("mika", "fill {x-9} {y1} {z-9} {x9} {y9} {z9} minecraft:air")
        assert placed == ["fill {x-9} {y1} {z-9} {x9} {y9} {z9} minecraft:air"]

    asyncio.run(run())


def test_hand_blocks_and_sounds():
    spots, block = mm.hand_blocks("fill 1 64 1 3 64 1 minecraft:oak_planks")
    assert spots == [(1, 64, 1), (2, 64, 1), (3, 64, 1)] and block == "minecraft:oak_planks"
    assert mm.hand_blocks("setblock 5 70 5 minecraft:oak_stairs[facing=east]")[0] == [(5, 70, 5)]
    assert mm.hand_blocks("fill 0 0 0 9 9 9 minecraft:stone") is None  # too big for one run
    assert mm.hand_blocks("fill 0 0 0 1 1 1 minecraft:stone replace minecraft:dirt") is None
    assert mm.hand_blocks("summon minecraft:cow 1 2 3") is None
    assert len(mm.hand_blocks("fill 0 0 0 2 2 2 minecraft:stone outline")[0]) == 26  # not the middle
    assert mm.hand_item("minecraft:oak_stairs[facing=east]") == "oak_stairs"
    assert mm.hand_item("minecraft:wall_torch[facing=north]") == "torch"
    assert mm.place_sound("minecraft:glass") == "block.glass.place"
    assert mm.place_sound("minecraft:cherry_planks") == "block.wood.place"
    assert mm.place_sound("minecraft:quartz_block") == "block.stone.place"


def test_lay_blocks_command_is_patched_in():
    assert "name: '!layBlocks'" in mm.FLY_COMMANDS and "[VR] put " in mm.FLY_COMMANDS
    assert "(v14)" in mm.FLY_COMMANDS


def test_both_girls_never_get_the_same_spot(monkeypatch):
    """"Go to the garden" for both used to put them in one point: Mika's eyes
    (the stream camera) then showed only Luna's face."""
    async def run():
        eng = _live_engine(monkeypatch)
        eng.projects.state.update({"base": [8, 62, -81]})
        sent = []

        async def command(cid, text):
            sent.append((cid, text))
            return True

        eng._command = command
        view, focus = mm.PLACES["garden"]
        await eng._fly("mika", view, focus)
        await eng._fly("luna", view, focus)
        (mx, _my, mz), (lx, _ly, lz) = eng._target["mika"], eng._target["luna"]
        assert ((mx - lx) ** 2 + (mz - lz) ** 2) ** 0.5 >= 2 * mm.SIDE_GAP - 0.01
        mika_spot = eng._target["mika"]
        # Luna hovers right where Mika's spot is: Mika (the camera) keeps her
        # spot (both dodging each other made the picture jump left and right);
        # Luna is the one who moves aside
        eng._target.clear()
        eng._pos["luna"] = mika_spot
        await eng._fly("mika", view, focus)
        assert eng._target["mika"] == mika_spot
        await eng._fly("luna", view, focus)
        lx, _ly, lz = eng._target["luna"]
        assert ((mika_spot[0] - lx) ** 2 + (mika_spot[2] - lz) ** 2) ** 0.5 >= mm.PERSONAL_SPACE

    asyncio.run(run())


def test_overlapping_girls_are_parted(monkeypatch):
    async def run():
        eng = _live_engine(monkeypatch)
        sent = []

        async def command(cid, text):
            sent.append((cid, text))
            return True

        eng._command = command
        now = mm.time.time()
        eng._pos = {"mika": (10.0, 70.0, 10.0), "luna": (10.4, 70.0, 10.2)}
        eng._seen_at = {"mika": now, "luna": now}
        await eng._part_if_overlapping()
        assert not sent  # a moment of overlap is fine
        eng._close_since = now - 3
        await eng._part_if_overlapping()
        assert sent and sent[0][0] == "luna" and sent[0][1].startswith("!flyTo(")  # never the camera girl
        tx, _ty, tz = eng._target["luna"]
        assert ((tx - 10) ** 2 + (tz - 10) ** 2) ** 0.5 >= mm.PART_DISTANCE - 0.01

    asyncio.run(run())


def test_bring_into_view_never_stacks_them(monkeypatch):
    async def run():
        commands = []

        async def rcon(cmd, reply=False):
            commands.append(cmd)
            return "No entity was found"  # no free spot anywhere

        monkeypatch.setattr(mm, "rcon_command", rcon)
        assert await mm.bring_into_view("Luna", "Mika") is False
        assert "tp Luna Mika" not in commands

    asyncio.run(run())


def test_a_flood_of_comments_is_answered_in_batches(monkeypatch):
    """30 viewers at once: one AI call at a time, up to three viewers per answer,
    newcomers first, nothing piles up behind old answers."""
    async def run():
        eng = _live_engine(monkeypatch)
        calls = []

        async def reply_text(cid, author, text, more=None):
            calls.append([author] + [a for a, _t in more or []])
            await asyncio.sleep(0.01)
            return f"Hi {author}!"

        eng.reply_text = reply_text
        for i in range(30):
            eng.enqueue(f"viewer{i}", f"Luna build a tower {i}")
        for _ in range(200):
            await asyncio.sleep(0.01)
            if eng.viewer_lines:
                eng.viewer_lines.popleft()  # she speaks them
            if not eng.chat_queue and eng._answer_task and eng._answer_task.done():
                break
        answered = [a for call in calls for a in call]
        assert len(answered) == 30 and len(set(answered)) == 30  # everyone once
        assert len(calls) <= 11 and max(len(c) for c in calls) == mm.ANSWER_BATCH

    asyncio.run(run())


def test_chat_is_answered_fairly_and_nothing_is_dropped_too_soon(monkeypatch):
    eng = _engine()
    now = mm.time.time()
    eng.chat_queue.extend([
        {"who": "luna", "author": "ancient", "text": "hi", "at": now - mm.CHAT_MAX_AGE - 5, "first": True},
        {"who": "luna", "author": "regular", "text": "hi", "at": now - 30, "first": False},
        {"who": "luna", "author": "new", "text": "hi", "at": now - 1, "first": True},
    ])
    batch = eng._next_batch()
    # the regular who waited 30 s goes first; a newcomer only gets a small head start
    assert [c["author"] for c in batch] == ["regular", "new"]

def test_a_bot_gets_a_busy_chat_in_one_message(monkeypatch):
    async def run():
        eng = _live_engine(monkeypatch)
        eng.link.connected.set()
        now = mm.time.time()
        for i in range(3):
            eng.outbox.append({"to": "luna", "from": f"v{i}", "text": f"hello {i}", "at": now})
        task = asyncio.create_task(eng._deliver_loop())
        await asyncio.sleep(0.7)
        task.cancel()
        luna = [m for m in eng.link.sent if m[0] == "Luna"]
        assert len(luna) == 1 and all(f"v{i}: hello {i}" in luna[0][1]["message"] for i in range(3))

    asyncio.run(run())


def test_she_goes_back_to_building_after_a_viewer_request(monkeypatch):
    async def run():
        eng = _live_engine(monkeypatch)
        eng.projects.state.update({"base": [8, 62, -81]})
        flights = []

        async def fly(cid, view, focus):
            flights.append((cid, view))
            return 0.0

        async def arrive(*a):
            return None

        clock = [1000.0]
        monkeypatch.setattr(mm.time, "time", lambda: clock[0])

        async def tick(_s):
            clock[0] += 5.0  # time passes while the build waits for her

        monkeypatch.setattr(mm.asyncio, "sleep", tick)
        eng._fly, eng._arrive = fly, arrive
        eng._chose_at["luna"] = 1000.0  # a viewer just sent her to the castle
        assert await eng._back_to_work("luna", (1, 2, 3), (0, 0, 0))
        assert clock[0] - 1000.0 >= mm.CHOICE_HOLD and flights == [("luna", (1, 2, 3))]
        assert eng.lines and eng.lines[0]["text"] in mm.EXCLAIM["back"]
        assert not await eng._back_to_work("luna", (1, 2, 3), (0, 0, 0))  # not busy: no wait

    asyncio.run(run())


def test_a_wrong_guess_gets_an_argh(monkeypatch):
    eng = _engine()
    eng._exclaim("luna", "wrong", {"digit": 4, "guess": 9})
    text = eng.lines[0]["text"]
    assert "9" in text and "{" not in text  # the wrong guess, filled in
    eng._exclaim("mika", "done")  # too soon after the last one: quiet
    assert len(eng.lines) == 1


class _Chunk:
    def __init__(self, text):
        self.choices = [NS(delta=NS(content=text))]


class _StreamingLLM:
    """A fake OpenAI client that writes its answer a few words at a time."""

    def __init__(self, pieces, gate):
        self.pieces, self.gate = pieces, gate
        self.chat = NS(completions=NS(create=self.create))
        self.kwargs = None

    async def create(self, **kwargs):
        self.kwargs = kwargs

        async def gen():
            for i, piece in enumerate(self.pieces):
                if i == 3:
                    await self.gate.wait()  # the rest is still being written
                yield _Chunk(piece)

        return gen()


def test_she_starts_speaking_before_the_answer_is_written(monkeypatch):
    async def run():
        eng = _live_engine(monkeypatch)
        monkeypatch.setenv("OPENAI_API_KEY", "test")
        gate = asyncio.Event()
        eng._llm = _StreamingLLM(
            ["Argh!!! MathUnlocked, you ", "want the castle? ", "Fine, ", "I'm flying ", "there right now. ", "Hold on!"], gate)
        eng.lines.append({"who": "mika", "text": "bot chatter", "at": mm.time.time()})
        spoken = []

        async def say(cid, text, mood="", short=False):
            spoken.append((cid, text, mood))

        eng._say = say
        eng.enqueue("MathUnlockedYT", "Luna fly to the castle")
        speaker = asyncio.create_task(eng._speak_loop())  # chatter is waiting: the answer still goes first
        for _ in range(50):
            await asyncio.sleep(0.01)
            if spoken:
                break
        # the first sentence is out while the rest is not written yet
        assert spoken == [("luna", "Argh!!! MathUnlocked, you want the castle?", "lose")]
        await asyncio.sleep(0.1)
        assert len(spoken) == 1  # the bot chatter never cuts into her answer
        gate.set()
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(spoken) >= 3:
                break
        speaker.cancel()
        assert spoken[1] == ("luna", "Fine, I'm flying there right now. Hold on!", "")  # one piece, no new reaction
        assert spoken[2][1] == "bot chatter"
        assert eng._llm.kwargs["stream"] is True
        # her bot hears the comment AND her own words: what she does matches what she said
        note = eng.outbox[-1]
        assert note["to"] == "luna" and 'You already answered out loud: "Argh!!! MathUnlocked' in note["text"]
        # and both girls' shared memory has it
        assert "Luna told MathUnlockedYT" in eng._memory_text()

    asyncio.run(run())


def test_answers_know_what_just_happened(monkeypatch):
    eng = _engine()
    eng._remember("Mika said: I flew to the castle")
    eng._remember("Luna: network guessed wrong (9 for a 4)")
    system, user = eng._reply_prompt("luna", "Viewer", "what happened?", [])
    assert "I flew to the castle" in system and "9 for a 4" in system and "never contradict" in system
    assert user == "Viewer wrote: what happened?"
    assert "not an assistant" in system  # her character, not a helper


def test_regular_viewers_are_remembered_between_streams(tmp_path):
    path = tmp_path / "viewers.json"
    first = mm.ViewerMemory(path)
    first.saw("MathUnlockedYT", "build more layers!")
    assert "new here" in first.describe("MathUnlockedYT")
    first.save()
    second = mm.ViewerMemory(path)  # the next stream
    second.saw("MathUnlockedYT", "hi again")
    text = second.describe("MathUnlockedYT")
    assert "regular" in text and "number 2" in text and "build more layers" in text



def test_the_behind_camera_floats_behind_mika(monkeypatch):
    """Like third person from behind: her back and where she goes, never her face."""
    async def run():
        monkeypatch.setenv("VR_MINECRAFT_PLAYER", "Selwyn")
        monkeypatch.setenv("VR_MINECRAFT_CAMERA", "behind")
        eng = _live_engine(monkeypatch)
        commands = []

        async def rcon(cmd, reply=False):
            commands.append(cmd)
            if cmd == "data get entity Mika Pos":
                return "Mika has the following entity data: [100.5d, 70.0d, 200.5d]"
            if cmd == "data get entity Mika Rotation":
                return "Mika has the following entity data: [0.0f, 10.0f]"  # facing +z
            return "ok"

        async def who():
            return {"mika", "luna", "selwyn"}

        monkeypatch.setattr(mm, "rcon_command", rcon)
        monkeypatch.setattr(mm, "rcon_online", who)
        assert eng.camera_style == "behind"
        await eng._camera_tick()
        assert not any(c.startswith("spectate Mika") for c in commands)  # not her eyes
        assert any(c.startswith("summon minecraft:armor_stand") for c in commands)
        assert any(c.startswith("spectate @e[tag=vr_cam") for c in commands)
        (cx, cy, cz), (lx, ly, lz) = eng._shot
        assert (cx, cy, cz) == (100.5, 70.0 + mm.CHASE_UP, 200.5 - mm.CHASE_BACK)  # behind (-z) and above
        assert lz > 200.5 and cy > ly  # looking past her, down at her back

    asyncio.run(run())


def test_show_me_moves_the_camera_and_the_network_stays_quiet(monkeypatch):
    async def run():
        eng = _live_engine(monkeypatch)
        eng.projects.state.update({"base": [0, 64, 0]})
        flights = []

        async def fly(cid, view, focus):
            flights.append((cid, view, focus))
            return 0.0

        async def arrive(*a):
            return None

        eng._fly, eng._arrive = fly, arrive
        assert mm.place_ask("Mika let's go to the front of the neural network") == "network"
        assert mm.place_ask("I want to see the castle") == "castle"
        assert mm.place_ask("nice castle") == ""
        assert mm.FURTHER_ASK.search("go a little further from it I wanna see it in further view")
        eng.enqueue("MathUnlockedYT", "I want to see the neural network")
        await asyncio.sleep(0.05)
        assert flights and flights[-1][0] == "mika" and flights[-1][1] == mm.PLACES["network"][0]  # Mika = the camera
        assert eng._chose_at["mika"] > mm.time.time()  # the build leaves her there for a while
        # the girls cannot keep testing the network themselves
        await eng._heard("Mika", "[VR] testNetwork 3")
        await eng._heard("Mika", "[VR] testNetwork 4")
        assert eng.link.sent[-1][1]["message"].startswith("Not now: the network was just tested")

    asyncio.run(run())


def test_camera_girl_never_dodges_while_building():
    """Mika's eyes are the stream: she never sidesteps for Luna, Luna does."""
    eng = _engine()
    eng._pos["luna"] = (10.0, 70.0, 10.0)
    assert eng._hop_spot("mika", 10.0, 70.0, 10.0, 20.0, 10.0) == (10.0, 70.0, 10.0)
    eng._pos["mika"] = (10.0, 70.0, 10.0)
    x, _y, z = eng._hop_spot("luna", 10.0, 70.0, 10.0, 20.0, 10.0)
    assert ((x - 10) ** 2 + (z - 10) ** 2) ** 0.5 >= mm.PERSONAL_SPACE


def test_same_flight_twice_is_sent_once(monkeypatch):
    async def run():
        eng = _live_engine(monkeypatch)
        fly = "!flyTo(1.0, 70.0, 2.0, 5.0, 70.0, 5.0, -60)"
        await eng._command("mika", fly)
        await eng._command("mika", fly)
        assert len(eng.link.sent) == 1
        await eng._command("mika", "!flyTo(9.0, 70.0, 2.0, 5.0, 70.0, 5.0, -60)")
        assert len(eng.link.sent) == 2
        eng._last_flight["mika"] = (fly, mm.time.time() - mm.REPEAT_FLIGHT - 1)
        await eng._command("mika", fly)  # long after: sent again
        assert len(eng.link.sent) == 3

    asyncio.run(run())


def test_movement_check_is_switched_off(monkeypatch):
    async def run():
        commands = []

        async def rcon(cmd, reply=False):
            commands.append(cmd)
            return ""

        monkeypatch.setattr(mm, "rcon_command", rcon)
        monkeypatch.setenv("VR_MINECRAFT_DAYLIGHT", "0")
        eng = _live_engine(monkeypatch)
        await eng._keep_daylight()
        assert "gamerule disablePlayerMovementCheck true" in commands
        assert "time set day" not in commands

    asyncio.run(run())


def test_flight_stops_fighting_the_server():
    js = mm.FLY_COMMANDS
    assert "forcedMove" in js and "[VR] pulledBack" in js and "removeListener('forcedMove'" in js


def test_messages_wait_for_a_bot_that_is_still_joining(tmp_path, monkeypatch):
    """'respondFunc is not a function': a command sent while she was still
    joining was lost. The patch makes it wait for her."""
    proxy = tmp_path / "src/agent/mindserver_proxy.js"
    proxy.parent.mkdir(parents=True)
    proxy.write_text("before\n" + mm.SEND_ORIGINAL + "\nafter\n")
    monkeypatch.setattr(mm, "MINDCRAFT_DIR", tmp_path)
    assert "mindserver_proxy.js" in mm.patch_mindcraft()
    text = proxy.read_text()
    assert "typeof this.agent.respondFunc !== 'function'" in text
    assert mm.patch_mindcraft() == []  # once only
