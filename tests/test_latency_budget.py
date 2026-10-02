"""Latency is a call-count problem, so count the calls.

Chat became slow because every single comment ran a big code-generation call,
then a second "gate" call to review it, then the conversation call. Three
model round trips to say hello.

The budget now:

    ordinary chat   1 small decision + 1 conversation turn
    run             1 small decision + 1 conversation turn   (no generation)
    create/modify   1 small decision + 1 generation + 1 conversation turn

The conversation turn is stubbed here, so these count the extra calls the
coding system adds on top of simply talking.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from tests.test_code_in_public import HEART_PY, Stage  # noqa: E402


def calls(stage: Stage) -> tuple[int, int]:
    return stage.llm.decide_calls, stage.llm.generate_calls


def test_ordinary_chat_costs_one_small_decision_and_nothing_else():
    stage = Stage()
    stage.start_session("luna")
    stage.say("luna this is so cool")
    assert calls(stage) == (1, 0)


def test_chat_outside_a_coding_session_costs_nothing_extra_beyond_the_decision():
    stage = Stage()
    stage.say("mika how are you?")
    decide, generate = calls(stage)
    assert generate == 0
    assert decide <= 1


def test_run_never_pays_for_code_generation():
    stage = Stage()
    stage.start_session("luna")
    stage.lesson.set_artifact("dev:selwyn", "python", HEART_PY % "red")
    stage.decide("run it", '{"action":"run"}')
    stage.say("run it")
    assert calls(stage) == (1, 0)


def test_a_change_pays_for_exactly_one_generation():
    stage = Stage()
    stage.start_session("luna")
    stage.lesson.set_artifact("dev:selwyn", "python", HEART_PY % "red")
    stage.decide("green", '{"action":"modify","language":"python"}')
    stage.code(HEART_PY % "green")
    stage.say("make it green")
    assert calls(stage) == (1, 1)


def test_ten_chat_messages_never_generate_code():
    stage = Stage()
    stage.start_session("luna")
    stage.lesson.set_artifact("dev:selwyn", "python", HEART_PY % "red")
    for i in range(10):
        stage.say(f"nice work number {i}")
    decide, generate = calls(stage)
    assert generate == 0, "chat was still paying for code generation"
    assert decide == 10
