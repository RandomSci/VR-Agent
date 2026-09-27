"""One world, one authoritative state: what is on screen = what RoomState
says = what Mika and Luna are told. No LLM is involved in any of it."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from open_llm_vtuber.room import awareness
from open_llm_vtuber.room.live_message import LiveMessage
from open_llm_vtuber.room.profiles import load_room
from open_llm_vtuber.room.session import RoomSession
from open_llm_vtuber.room.state import SceneState
from open_llm_vtuber.vr_agent.usage import usage

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):
        self.t = time.time()

    def __call__(self):
        return self.t


def make_session():
    clock = Clock()
    session = RoomSession(load_room(ROOT / "room", ROOT), clock=clock)
    pushed: list[dict] = []

    async def push(ops):
        pushed.extend(op for op in ops if op)

    session.push = push  # what the OBS page would receive
    return session, clock, pushed


def viewer(text: str, user: str = "Selwyn") -> LiveMessage:
    return LiveMessage("youtube", f"m-{time.time_ns()}", user, text, time.time())


def pond_scene() -> SceneState:
    return SceneState(
        id="deep_forest_pond",
        name="the Deep Forest, by a quiet pond",
        env="forest",
        region="deep_forest",
        time="night",
        weather="fog",
        zones={"pond": 0.8},
        zone_labels={"pond": "the pond"},
    )


def test_frog_exists_is_known_vanishes_by_magic_and_is_remembered():
    usage.reset()
    session, clock, pushed = make_session()
    # 1. forest pond scene
    ops = session.world.set_scene(pond_scene())
    assert ops[-1]["op"] == "scene" and ops[-1]["scene"]["id"] == "deep_forest_pond"
    # 2. frog in the right pond zone
    ops = session.world.spawn_object(
        "frog", zone="pond", object_id="frog", note="a frog hopped out by the pond"
    )
    frog_op = next(op for op in ops if op["op"] == "object")
    # 3. renderer-facing state says the frog is visible
    assert (
        frog_op["type"] == "frog" and frog_op["visible"] and frog_op["zone"] == "pond"
    )
    assert session.state.snapshot()["objects"]["frog"]["visible"]
    # 4. the characters know it exists
    context = awareness.build(session, "luna")
    assert "a frog on the right" in context
    assert "Deep Forest" in context
    # 5. deterministic Mika magic on the frog
    outcome = session.interactions.cast_on("mika", "frog")
    assert outcome.performed
    # 6. the action and its effect happen: look, spell motion, bolt, sound
    kinds = {(op["op"], op.get("name") or op.get("target")) for op in outcome.ops}
    assert ("attention", "OBJECT:frog") in kinds
    assert ("action", "magic_heart") in kinds
    assert ("world_fx", "magic_bolt") in kinds and ("world_sfx", "magic") in kinds
    assert "frog" in session.state.objects  # not yet: the bolt is still flying
    # 7. when the bolt lands, the frog is gone from the state
    clock.t += 2.5
    landed = session.tick()
    assert "frog" not in session.state.objects
    # 8. renderer-facing update: the frog is removed with a poof
    assert any(
        op["op"] == "object" and op["id"] == "frog" and op.get("remove")
        for op in landed
    )
    assert any(
        op["op"] == "action" and op["character"] == "luna" for op in landed
    )  # Luna reacts
    # 9. recent history records the disappearance
    texts = [e.text for e in session.state.history]
    assert any("Mika's spell made a frog vanish" in t for t in texts)
    # 10. the characters no longer think it is visible, but know what happened
    context = awareness.build(session, "luna")
    assert "Visible right now" not in context or "a frog on the right" not in context
    assert "Mika's spell made a frog vanish" in context
    assert usage.snapshot()["llm_requests"] == 0


def test_walk_towards_the_center_is_performed_and_she_is_told_she_is_doing_it():
    session, clock, pushed = make_session()

    async def go():
        message = viewer("walk towards the center")
        consumed = session.observe_viewer_message(message)
        await asyncio.sleep(0)
        return message, consumed

    message, consumed = asyncio.run(go())
    assert consumed is False  # she still answers
    mika = session.state.characters["mika"]
    assert mika.zone == "center" and abs(mika.x - 0.5) < 0.05
    stage = [op for op in pushed if op["op"] == "stage"]
    assert stage and stage[0]["character"] == "mika" and stage[0]["move"] == "walk"
    plan = session.director.plan(message)
    assert plan.turns[0].speaker == "mika"
    prompt = session.director._prompt(plan, 0)
    assert "The stream is doing it for you right now" in prompt
    assert "never claim you cannot" in prompt
    assert "move to a spot on stage" in prompt
    assert "You cannot do anything else physically" in prompt  # the honest limit


def test_named_character_moves_and_the_other_steps_aside():
    session, clock, pushed = make_session()
    ok, reason, ops = session.stage.move_to("luna", zone="far_left")
    assert ok
    mika, luna = session.state.characters["mika"], session.state.characters["luna"]
    assert abs(mika.x - luna.x) >= 0.199  # never overlapping


def test_requests_are_refused_truthfully_during_a_game_and_for_missing_things():
    session, clock, pushed = make_session()

    async def go():
        session.observe_viewer_message(viewer("play tic tac toe"))
        message = viewer("Luna go to the left")
        session.observe_viewer_message(message)
        return message

    message = asyncio.run(go())
    plan = session.director.plan(message)
    prompt = session.director._prompt(plan, 0)
    assert "a game is on the board" in prompt and "not happening" in prompt
    assert session.state.characters["luna"].zone != "left"

    session2, _, _ = make_session()
    message = viewer("Mika make the frog disappear")
    asyncio.run(asyncio.sleep(0))
    session2.observe_viewer_message(message)
    plan = session2.director.plan(message)
    prompt = session2.director._prompt(plan, 0)
    assert "there is no frog here right now" in prompt


def test_jump_and_dance_requests_use_the_stage_not_a_false_no():
    session, clock, pushed = make_session()

    async def go(text):
        message = viewer(text)
        session.observe_viewer_message(message)
        await asyncio.sleep(0)
        return message

    message = asyncio.run(go("Luna can you jump?"))
    assert any(op["op"] == "stage" and op["move"] == "hop" for op in pushed)
    prompt = session.director._prompt(session.director.plan(message), 0)
    assert "doing a little hop right now" in prompt


def test_tictactoe_awareness_matches_the_game_state():
    session, clock, pushed = make_session()
    asyncio.run(asyncio.sleep(0))
    session.observe_viewer_message(viewer("Luna, play tic tac toe with us"))
    game = session.show.engine.active
    game.marks = {"viewers": "X", "luna": "O"}
    game.cells[4] = "X"
    game.cells[0] = "O"
    game.last_move = 0
    game.turn = "viewers"
    game.phase = "vote"
    text = awareness.describe_game(session, "luna")
    assert "Tic-Tac-Toe" in text
    assert "chat plays X" in text and "Luna plays O" in text
    assert "X in 5" in text and "O in 1" in text
    assert "It is chat's turn" in text
    assert "You are playing (against chat)" in text
    other = awareness.describe_game(session, "mika")
    assert "You are not playing this one" in other


def test_trivia_awareness_never_leaks_the_answer_before_the_reveal():
    session, clock, pushed = make_session()
    session.observe_viewer_message(viewer("play trivia"))
    game = session.show.engine.active
    game.question = {"question": "Which planet is red?", "correct_answer": "Mars"}
    game.phase = "question"
    text = awareness.describe_game(session, "luna")
    assert "Mars" not in text and "answer window is open" in text
    game.phase = "reveal"
    assert "Answer revealed: Mars" in awareness.describe_game(session, "luna")


def test_history_and_objects_stay_bounded():
    session, clock, pushed = make_session()
    for i in range(500):
        session.state.record("test", f"event {i}")
        session.world.spawn_object("rock", zone="left", note="")
    assert len(session.state.history) <= 16
    assert len(session.state.objects) <= session.state.MAX_OBJECTS
    assert len(awareness.build(session, "mika")) < 2500


def test_scene_change_clears_old_scene_objects():
    session, clock, pushed = make_session()
    session.world.set_scene(pond_scene())
    session.world.spawn_object("frog", zone="pond", object_id="frog")
    session.world.spawn_object("mushrooms", zone="left", object_id="shrooms")
    ops = session.world.set_scene(
        SceneState(id="mountain_trail", name="a misty mountain trail", env="mountain")
    )
    removed = {op["id"] for op in ops if op.get("remove")}
    assert removed == {"frog", "shrooms"}
    assert session.state.visible_objects() == []
    assert "game_board" in session.state.objects  # permanent anchor untouched
