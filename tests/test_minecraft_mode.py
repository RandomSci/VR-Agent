from types import SimpleNamespace as NS

from src.open_llm_vtuber.room import minecraft_mode as mm


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
        assert eng.camera_on and commands[:3] == [
            "gamemode creative Selwyn", "gamemode spectator Selwyn", "spectate Mika Selwyn"
        ]
        assert {"kind": "camera", "mode": "client"} in eng.pushed
        eng._seen_at["luna"] = clock[0]
        await eng._camera_to("luna")
        assert commands[-1] == "spectate Mika Selwyn"  # held, Mika only just got it
        clock[0] += mm.CAMERA_HOLD + 1
        eng._seen_at["luna"] = clock[0]
        await eng._camera_to("luna")
        assert commands[-1] == "spectate Luna Selwyn" and {"kind": "focus", "who": "luna"} in eng.pushed
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
        monkeypatch.delenv("VR_MINECRAFT_CAMERA", raising=False)
        eng = _live_engine(monkeypatch)
        clock = [1000.0]
        monkeypatch.setattr(mm.time, "time", lambda: clock[0])

        async def no_wait(_s):
            return None

        monkeypatch.setattr(mm.asyncio, "sleep", no_wait)
        commands = []

        async def rcon(cmd, reply=False):
            commands.append(cmd)
            return True

        async def who():
            return {"mika", "luna", "selwyn"}

        monkeypatch.setattr(mm, "rcon_command", rcon)
        monkeypatch.setattr(mm, "rcon_online", who)
        for cid, pos in (("mika", (0.0, 70.0, 0.0)), ("luna", (0.0, 70.0, 6.0))):
            eng._pos[cid] = pos
            eng._seen_at[cid] = clock[0]
        eng.build_focus = (20.0, 72.0, 3.0)
        await eng._camera_tick()
        assert not any(c.startswith("spectate") for c in commands)
        tp = [c for c in commands if c.startswith("tp Selwyn")]
        assert len(tp) == 1 and " facing " in tp[0]
        cx, cy, cz = map(float, tp[0].split()[2:5])
        assert cx < 0 and cy > 70  # behind and above the girls, looking at them and the build
        await eng._camera_tick()
        assert len([c for c in commands if c.startswith("tp Selwyn")]) == 1  # nothing moved: same shot
        clock[0] += mm.CAMERA_SHOT_SECONDS + 1
        eng._seen_at = {"mika": clock[0], "luna": clock[0]}
        eng.build_focus = (-30.0, 72.0, 3.0)  # the next piece is on the other side
        await eng._camera_tick()
        glide = [c for c in commands if c.startswith("tp Selwyn")][1:]
        assert len(glide) == int(mm.CAMERA_MOVE[0] * mm.CAMERA_MOVE[1])  # a smooth move, not a jump
        assert float(glide[-1].split()[2]) > 0  # now from the other side

    asyncio.run(run())
