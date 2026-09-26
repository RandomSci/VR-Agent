"""Room config and character profile validation."""

import shutil
from pathlib import Path

import pytest
import yaml

from open_llm_vtuber.room.profiles import load_room, parse_profile

ROOT = Path(__file__).resolve().parents[1]


def test_bundled_room_loads_mika_and_luna():
    room = load_room(ROOT / "room", ROOT)
    assert room.active and room.problems == []
    assert [c.id for c in room.characters] == ["mika", "luna"]
    mika, luna = room.characters
    assert mika.primary and not luna.primary
    assert mika.model == "mao_pro" and luna.model == "hiyori"
    assert mika.voice.tts_model is None  # inherits conf.yaml TTS
    assert luna.voice.tts_model == "edge_tts" and luna.voice.settings["voice"]
    assert mika.mouth == "ParamA" and luna.mouth == "ParamMouthOpenY"
    # Every reaction and emotion points at a real registry action.
    for profile in room.characters:
        for actions in profile.reactions.values():
            assert all(profile.capabilities.get(a) for a in actions)
        for action in profile.emotions.values():
            assert action is None or profile.capabilities.get(action)
    assert (
        "cheer" in luna.capabilities.actions
        and "summon_rabbit" in mika.capabilities.actions
    )
    assert room.objects["game_board"]["width"] > 0
    payload = room.to_frontend()
    assert payload["characters"][1]["model_url"].endswith("Hiyori.model3.json")
    assert payload["characters"][0]["look"]["angle_x"] == "ParamAngleX"


def test_hiyori_motions_are_real_and_idle_loop_is_calm():
    room = load_room(ROOT / "room", ROOT)
    luna = room.get("luna")
    caps = luna.capabilities
    assert caps.idle_group == "Idle" and caps.motion_groups["Idle"] == 1
    assert caps.get("side_eye").viewer_requestable is False
    assert caps.alternatives["wave"] == "cheer"
    assert set(caps.expression_names) >= {
        "neutral",
        "smile",
        "blush",
        "surprised",
        "pout",
    }


def copy_room(tmp_path: Path) -> Path:
    target = tmp_path / "room"
    shutil.copytree(ROOT / "room", target)
    return target


def edit(path: Path, **changes):
    data = yaml.safe_load(path.read_text())
    data.update(changes)
    path.write_text(yaml.safe_dump(data))


def test_character_with_unknown_model_is_dropped_and_room_continues(tmp_path):
    room_dir = copy_room(tmp_path)
    edit(room_dir / "characters" / "luna.yaml", model="does_not_exist")
    room = load_room(room_dir, ROOT)
    assert room.active and [c.id for c in room.characters] == ["mika"]
    assert any("does_not_exist" in p for p in room.problems)


def test_look_parameter_missing_from_model_drops_character(tmp_path):
    room_dir = copy_room(tmp_path)
    edit(room_dir / "characters" / "luna.yaml", look={"angle_x": "PARAM_ANGLE_X"})
    room = load_room(room_dir, ROOT)
    assert [c.id for c in room.characters] == ["mika"]
    assert any("PARAM_ANGLE_X" in p for p in room.problems)


def test_disabled_or_broken_room_is_inactive(tmp_path):
    room_dir = copy_room(tmp_path)
    edit(room_dir / "room.yaml", enabled=False)
    assert not load_room(room_dir, ROOT).active

    (room_dir / "room.yaml").write_text("- just a list\n")
    broken = load_room(room_dir, ROOT)
    assert not broken.active and broken.problems

    assert not load_room(tmp_path / "missing", ROOT).active


def test_primary_is_unique_and_aliases_cannot_collide(tmp_path):
    room_dir = copy_room(tmp_path)
    edit(room_dir / "characters" / "luna.yaml", primary=True, aliases=["mika", "moon"])
    room = load_room(room_dir, ROOT)
    assert [c.primary for c in room.characters] == [True, False]
    assert room.get("luna").aliases == ("moon",)
    assert any("ambiguous" in p for p in room.problems)


def test_autonomous_banter_setting_is_ignored(tmp_path):
    room_dir = copy_room(tmp_path)
    edit(room_dir / "room.yaml", director={"autonomous_banter": {"enabled": True}})
    room = load_room(room_dir, ROOT)
    assert not hasattr(room.director, "autonomous_enabled")


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"id": "Bad Id"}, "id"),
        ({"name": ""}, "name"),
        ({"persona": "short"}, "persona"),
        ({"mouth": "not a param!"}, "mouth"),
        ({"look": {"eye_x": "<script>"}}, "look"),
        ({"voice": {"tts_model": "rm -rf"}}, "voice"),
    ],
)
def test_invalid_profiles_are_rejected(changes, reason):
    raw = yaml.safe_load((ROOT / "room" / "characters" / "mika.yaml").read_text())
    raw.update(changes)
    with pytest.raises(ValueError):
        parse_profile(raw)


def test_game_line_templates_only_allow_the_answer_placeholder():
    raw = yaml.safe_load((ROOT / "room" / "characters" / "mika.yaml").read_text())
    raw["game"]["lines"]["answer"] = ["It's {answer}!", "{__class__}", "{answer.upper}"]
    profile = parse_profile(raw)
    assert profile.game_lines["answer"] == ("It's {answer}!",)
