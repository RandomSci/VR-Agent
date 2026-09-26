"""Attention, Action and World directors driven by room events.

Everything here is deterministic and local; no LLM or TTS is involved.
"""

import random
from pathlib import Path

import pytest

from open_llm_vtuber.room import events as ev
from open_llm_vtuber.room.profiles import load_room
from open_llm_vtuber.room.session import RoomSession
from open_llm_vtuber.vr_agent.usage import usage

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def room(clock):
    usage.reset()
    return RoomSession(
        load_room(ROOT / "room", ROOT), rng=random.Random(7), clock=clock
    )


def looks(ops):
    return {op["character"]: op["target"] for op in ops if op["op"] == "attention"}


def actions(ops):
    return {op["character"]: op["name"] for op in ops if op["op"] == "action"}


def test_listener_looks_at_the_speaker(room):
    ops = room.emit(ev.SPEECH_STARTED, character="luna", seconds=4)
    assert looks(ops) == {"luna": "VIEWER", "mika": "CHARACTER:luna"}
    listener = next(o for o in ops if o["character"] == "mika")
    assert listener["delay_ms"] > 0  # reacts a moment later, not instantly


def test_asking_the_other_character_makes_them_look_at_each_other(room):
    ops = room.emit(ev.SPEECH_STARTED, character="mika", addressee="luna", seconds=3)
    assert looks(ops) == {"mika": "CHARACTER:luna", "luna": "CHARACTER:mika"}


def test_viewer_addressing_everyone_turns_both_to_the_viewer(room):
    assert looks(room.emit(ev.VIEWER_ADDRESSED, targets=["all"])) == {
        "mika": "VIEWER",
        "luna": "VIEWER",
    }


def test_game_question_turns_both_to_the_board(room):
    assert looks(room.emit(ev.QUESTION_SHOWN)) == {
        "mika": "OBJECT:game_board",
        "luna": "OBJECT:game_board",
    }
    turn = looks(room.emit(ev.CHARACTER_TURN, player="mika"))
    assert turn == {"mika": "GAME", "luna": "CHARACTER:mika"}


def test_correct_answer_drives_attention_and_reactions(room):
    ops = room.emit(ev.ANSWER_CORRECT, player="luna")
    assert looks(ops) == {"luna": "VIEWER", "mika": "CHARACTER:luna"}
    acted = actions(ops)
    assert acted["luna"] == "smile"  # Luna's 'happy' reaction, an expression
    assert acted["mika"] == "surprised"


def test_viewer_scoring_turns_everyone_to_the_viewer(room):
    ops = room.emit(ev.ANSWER_CORRECT, player="viewers")
    assert looks(ops) == {"mika": "VIEWER", "luna": "VIEWER"}


def test_winner_celebrates_loser_reacts_and_objects_turn_heads(room):
    ops = room.emit(ev.GAME_FINISHED, winner="mika", loser="luna")
    acted = actions(ops)
    assert acted["luna"] in ("sad", "open_arms")
    if acted["mika"] == "magic_heart":
        # The heart becomes a room object that Luna looks at.
        objects = [o for o in ops if o["op"] == "object"]
        assert objects and objects[0]["id"] == "heart"
        assert any(
            o.get("target") == "OBJECT:heart" for o in ops if o["op"] == "attention"
        )


def test_summoned_rabbit_is_an_object_everyone_looks_at_then_it_expires(room, clock):
    ops = room.play("mika", "summon_rabbit")
    assert ops[0] == {
        "op": "action",
        "character": "mika",
        "name": "summon_rabbit",
        "delay_ms": 0,
        "sync": "now",
        "_followed": True,
    }
    rabbit = next(o for o in ops if o["op"] == "object")
    assert rabbit["id"] == "rabbit" and 0.0 < rabbit["x"] < 0.15 and rabbit["y"] < 0.3
    # Heads turn when the rabbit actually shows up during the motion.
    assert all(o["delay_ms"] >= 4000 for o in ops if o["op"] == "attention")
    assert looks(ops) == {"mika": "OBJECT:rabbit", "luna": "OBJECT:rabbit"}
    assert actions(ops)["luna"] == "surprised"
    assert "rabbit" in room.state.objects
    clock.t += 5
    assert room.tick() == []  # still visible
    clock.t += 5
    assert room.tick() == [{"op": "object", "id": "rabbit", "remove": True}]
    assert "rabbit" not in room.state.objects
    assert room.tick() == []


def test_reactions_have_a_cooldown(room, clock):
    first = room.actions.react("mika", "surprised")
    assert first and first["name"] == "surprised"
    assert room.actions.react("mika", "surprised") is None
    clock.t += 5
    assert room.actions.react("mika", "surprised") is not None


def test_unavailable_character_is_left_out(room):
    room._clients["page"] = None
    room.on_client_status("page", ["mika"], ["luna"])
    assert looks(room.emit(ev.QUESTION_SHOWN)) == {"mika": "OBJECT:game_board"}
    assert room.actions.react("luna", "happy") is None


def test_unknown_events_are_rejected_and_broken_rules_are_isolated(room):
    with pytest.raises(ValueError):
        room.emit("RUN_SHELL")

    def broken(event):
        raise RuntimeError("boom")

    room.bus.subscribe(ev.QUESTION_SHOWN, broken)
    assert len(looks(room.emit(ev.QUESTION_SHOWN))) == 2


def test_directors_never_touch_llm_or_tts(room, clock):
    for _ in range(50):
        room.emit(ev.SPEECH_STARTED, character="mika", seconds=2)
        room.emit(ev.ANSWER_CORRECT, player="luna")
        room.play("mika", "summon_rabbit")
        clock.t += 7
        room.tick()
    snap = usage.snapshot()
    assert snap["llm_requests"] == 0 and snap["tts_requests"] == 0
