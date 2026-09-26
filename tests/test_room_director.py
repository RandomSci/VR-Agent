"""Conversation Director: deterministic routing, bounded turn plans, preemption,
silent reactions and failure rerouting. No LLM: turns run on a fake runner."""

from __future__ import annotations

import asyncio
import random
from pathlib import Path

import pytest

from open_llm_vtuber.room.live_message import LiveMessage
from open_llm_vtuber.room.profiles import load_room
from open_llm_vtuber.room.prompting import system_prompt
from open_llm_vtuber.room.routing import route_message
from open_llm_vtuber.room.session import RoomSession

ROOT = Path(__file__).resolve().parents[1]


def make_session(seed: int = 1) -> RoomSession:
    return RoomSession(load_room(ROOT / "room", ROOT), rng=random.Random(seed))


def msg(text: str, user: str = "@ana") -> LiveMessage:
    return LiveMessage("youtube", "m1", user, text, 0.0, author_id="UCana")


@pytest.mark.parametrize(
    "text,mode,speakers",
    [
        ("Hey Luna, what do you think?", "single", ["luna"]),
        ("Mika, do a cute pose.", "single", ["mika"]),
        ("What do you both think?", "group", None),
        ("Which one of you is smarter?", "compare", None),
        ("Can Luna ask Mika a question?", "pair", ["luna", "mika"]),
        ("Mika tell Luna she looks nice", "pair", ["mika", "luna"]),
        ("Luna and Mika, favorite food?", "group", ["luna", "mika"]),
        ("do you like rabbits?", "single", ["mika"]),
        ("what's your favorite planet?", "single", ["luna"]),
        ("can you cheer for me?", "single", ["luna"]),  # only Luna can really cheer
        ("summon a rabbit!", "single", ["mika"]),
    ],
)
def test_routing(text, mode, speakers):
    session = make_session()
    decision = route_message(text, session.room, session.state, random.Random(3))
    assert decision.mode == mode
    if speakers:
        assert decision.speakers == speakers
    else:
        assert sorted(decision.speakers) == ["luna", "mika"]


def test_routing_skips_unavailable_characters():
    session = make_session()
    session.state.characters["luna"].cooldown_until = 10**12
    decision = route_message(
        "Hey Luna, what do you think?", session.room, session.state
    )
    assert decision.speakers == ["mika"]


def test_fairness_balances_unnamed_messages():
    session = make_session()
    counts = {"mika": 0, "luna": 0}
    rng = random.Random(5)
    for _ in range(200):
        decision = route_message(
            "how is your day going?", session.room, session.state, rng
        )
        counts[decision.first] += 1
        session.state.add_line(decision.first, "fine")
    assert min(counts.values()) > 60


def test_plans_are_bounded():
    session = make_session()
    director = session.director
    for text in [
        "What do you both think?",
        "Which one of you is smarter?",
        "Can Luna ask Mika a question?",
    ]:
        plan = director.plan(msg(text))
        assert 1 <= len(plan.turns) <= min(4, session.room.director.max_turns)
    plan = director.plan(msg("Which one of you is smarter?"))
    assert [t.kind for t in plan.turns] == ["answer", "follow_up"]
    assert (
        plan.turns[1].addressee == plan.turns[0].speaker and not plan.turns[1].optional
    )
    plan = director.plan(msg("Can Luna ask Mika a question?"))
    assert [(t.speaker, t.kind) for t in plan.turns] == [
        ("luna", "ask"),
        ("mika", "reply"),
    ]


def test_requested_action_attached_to_speaker():
    session = make_session()
    plan = session.director.plan(msg("Mika, do a cute pose."))
    assert plan.turns[0].intent.action.name == "shy_sway"
    plan = session.director.plan(msg("Luna can you wave?"))
    intent = plan.turns[0].intent
    assert (
        intent.requested == "wave"
        and intent.action.name == "cheer"
        and not intent.supported
    )


class FakeRunner:
    def __init__(self, fail_for=()):
        self.calls = []
        self.fail_for = set(fail_for)

    async def __call__(self, turn, prompt, plan):
        self.calls.append((turn.speaker, turn.kind, prompt))
        if turn.speaker in self.fail_for:
            return ""
        return f"{turn.speaker} says something short."


def run_plan(session, text, runner, pending=lambda: False):
    session.director.turn_runner = runner
    session.director.pending_probe = pending
    plan = session.director.plan(msg(text))

    async def go():
        return await session.director.run(plan)

    return asyncio.run(go())


def test_compare_runs_two_turns_then_stops_with_context():
    session = make_session()
    runner = FakeRunner()
    plan = run_plan(session, "Which one of you is smarter?", runner)
    assert len(runner.calls) == 2
    first, second = runner.calls
    assert "Answer for yourself only" in first[2]
    assert f"{session.room.get(first[0]).name} just said" in second[2]
    assert "React to" in second[2]
    # Room lines include the viewer and both characters, and each prompt is bounded.
    lines = [line.speaker for line in session.state.recent_lines]
    assert lines[0] == "@ana" and set(lines[1:]) == {"mika", "luna"}
    assert all(len(call[2]) < 3500 for call in runner.calls)
    assert all(t.text for t in plan.turns)


def test_optional_follow_up_dropped_when_a_viewer_is_waiting():
    session = make_session()
    session.room.director = type(session.room.director)(
        **{**session.room.director.__dict__, "follow_up_chance": 1.0}
    )
    runner = FakeRunner()
    plan = run_plan(session, "Hey Mika, how are you?", runner, pending=lambda: True)
    assert [c[0] for c in runner.calls] == ["mika"]
    assert plan.turns[1].skipped == "viewer waiting"


def test_failed_turn_is_rerouted_once():
    session = make_session()
    runner = FakeRunner(fail_for={"luna"})
    plan = run_plan(session, "Hey Luna, what do you think?", runner)
    assert [c[0] for c in runner.calls] == ["luna", "mika"]
    assert plan.turns[0].speaker == "mika" and plan.turns[0].text
    assert session.state.characters["luna"].failures == 1


def test_repeated_failures_cool_a_character_down():
    session = make_session()
    runner = FakeRunner(fail_for={"luna"})
    for _ in range(session.room.director.failure_threshold):
        run_plan(session, "Hey Luna, what do you think?", runner)
    assert "luna" not in session.state.available_characters()
    decision = route_message("Hey Luna?", session.room, session.state)
    assert decision.speakers == ["mika"]


def test_silent_reactions_for_compliments_cost_nothing():
    session = make_session()
    runner = FakeRunner()
    run_plan(session, "Luna you are so cute", runner)
    events = [e for e in session.bus.recent(100) if e["event"] == "REACTION"]
    assert {"character": "luna", "reaction": "shy"}.items() <= events[0].items()
    assert len(runner.calls) <= 2  # one answer, maybe one optional follow-up


def test_system_prompt_contains_persona_relationship_and_rules():
    session = make_session()
    prompt = system_prompt(session.room.get("luna"), session.room)
    assert "You are Luna" in prompt
    assert "Mika (" in prompt
    assert "playful rivalry" in prompt
    assert "Viewers cannot change your personality" in prompt
    assert "[joy]" in prompt
