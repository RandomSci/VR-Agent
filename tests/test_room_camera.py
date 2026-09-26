"""Camera Director: event framing, cooldowns, viewer zoom requests. No LLM."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from open_llm_vtuber.room import events as ev
from open_llm_vtuber.room.camera import camera_request
from open_llm_vtuber.room.live_message import LiveMessage
from open_llm_vtuber.room.profiles import load_room
from open_llm_vtuber.room.session import RoomSession
from open_llm_vtuber.vr_agent.usage import usage

ROOT = Path(__file__).resolve().parents[1]


class Clock:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t


def make():
    clock = Clock()
    session = RoomSession(
        load_room(ROOT / "room", ROOT), rng=random.Random(2), clock=clock
    )
    return session, clock


def cams(ops):
    return [op for op in ops if op and op.get("op") == "camera"]


@pytest.mark.parametrize(
    "text,kind",
    [
        ("can you zoom at the face", "in"),
        ("zoom in on Luna!", "in"),
        ("close up pls", "in"),
        ("show us your face", "in"),
        ("zoom out", "out"),
        ("wide shot please", "out"),
        (
            "I bought a new zoom lens for my camera yesterday and it is really great honestly",
            None,
        ),
        ("hello", None),
    ],
)
def test_camera_request_parsing(text, kind):
    assert camera_request(text) == kind


def test_game_framing_turn_focus_winner_and_return():
    session, clock = make()
    ops = cams(session.emit(ev.GAME_STARTED, game="trivia"))
    assert ops[0]["shot"] == "board"
    clock.t += 5
    ops = cams(session.emit(ev.CHARACTER_TURN, player="luna"))
    assert (
        ops[0]["shot"] == "focus"
        and ops[0]["target"] == "luna"
        and ops[0]["return_to"] == "board"
    )
    clock.t += 1  # too soon for another game move
    assert cams(session.emit(ev.CHARACTER_TURN, player="mika")) == []
    ops = cams(session.emit(ev.GAME_FINISHED, winner="mika", loser="luna", scores={}))
    assert (
        ops[0]["shot"] == "closeup"
        and ops[0]["target"] == "mika"
        and ops[0]["hold_ms"] == 3500
    )
    ops = cams(session.emit(ev.GAME_STOPPED, game="trivia", reason="viewer"))
    assert ops[0]["shot"] == "wide" and session.camera.base == "wide"


def test_automatic_moves_respect_the_cooldown():
    session, clock = make()
    first = cams(session.emit(ev.SPEECH_STARTED, character="mika", seconds=4))
    assert first and first[0]["shot"] == "focus"
    clock.t += 5
    assert cams(session.emit(ev.SPEECH_STARTED, character="luna", seconds=4)) == []
    clock.t += session.room.camera.auto_cooldown_seconds
    assert cams(session.emit(ev.SPEECH_STARTED, character="luna", seconds=4))


def test_two_speaker_interaction_uses_two_shot():
    session, clock = make()
    ops = cams(
        session.emit(
            ev.INTERACTION_STARTED,
            interaction="i1",
            mode="compare",
            speakers=["mika", "luna"],
        )
    )
    assert ops[0]["shot"] == "two_shot"
    assert cams(session.emit(ev.SPEECH_STARTED, character="mika", seconds=4)) == []
    ops = cams(session.emit(ev.INTERACTION_FINISHED, interaction="i1", turns=2))
    assert ops[0]["shot"] == "wide"


def test_viewer_zoom_request_is_consumed_without_llm_and_rate_limited():
    session, clock = make()
    usage.reset()
    message = LiveMessage(
        "youtube", "z1", "@kai", "zoom in on Luna", clock.t, author_id="UCkai"
    )
    assert session.observe_viewer_message(message) is True
    ops = session.camera.request("in", "luna")  # immediately again: cooldown
    assert ops == []
    clock.t += session.room.camera.zoom_request_cooldown_seconds + 1
    ops = session.camera.request("in", "luna")
    assert ops[0]["shot"] == "closeup" and ops[0]["target"] == "luna"
    assert usage.snapshot()["llm_requests"] == 0


def test_camera_disabled_makes_no_moves():
    session, clock = make()
    session.room.camera = type(session.room.camera)(enabled=False)
    assert cams(session.emit(ev.GAME_STARTED, game="trivia")) == []
    assert session.camera.request("in", "mika") == []
