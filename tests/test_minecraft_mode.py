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
