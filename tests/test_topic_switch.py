"""Changing the subject must start a new program, not edit the old one.

Regression from a real DEV session: a twinkling-stars matplotlib program was
on screen, the viewer said they wanted to learn Python lists, then "yeah
update the code" five times, and every time the twinkle program was edited
again ("why is that magical twinkle? I don't want to learn matplotlib").

Causes fixed:
* a request for a new program was forced into "modify" whenever code existed;
* the code writer only saw "yeah update the code", not what "it" was;
* the old program's requirements ("make it twinkle") were carried over;
* the lesson goal never moved on.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from open_llm_vtuber.room.coding_actions import (  # noqa: E402
    CodingDecision,
    DECIDE_SYSTEM,
    parse_decision,
)
from open_llm_vtuber.room.runtime import resolve_program_target  # noqa: E402
from tests.test_code_in_public import Stage  # noqa: E402

TWINKLE_PY = """import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
# Magical twinkle: stars and wizards
x, y = np.random.rand(2, 60)
plt.scatter(x, y, s=8, color="white")
plt.title("Magical Twinkle")
plt.savefig("output.png")
"""

LISTS_PY = """snacks = ["chips", "mango", "ube cake"]
print("Start:", snacks)
snacks.append("taho")
print("After append:", snacks)
snacks.remove("chips")
print("After remove:", snacks)
print("First snack:", snacks[0])
"""


class RecordingStage(Stage):
    """The real pipeline, recording exactly what the code writer was given."""

    def __init__(self):
        super().__init__()
        self.generate_requests: list[dict] = []
        llm = self.llm
        original = llm.chat_completion

        async def chat_completion(messages, system=None, tools=None):
            blob = "\n".join(str(m.get("content") or "") for m in messages)
            if DECIDE_SYSTEM[:60] not in blob:
                self.generate_requests.append(json.loads(messages[-1]["content"]))
                async for chunk in original(messages, system=system, tools=tools):
                    yield chunk
                return
            # The decision now also sees the program and recent chat, so
            # match the script against the latest comment only.
            latest = json.loads(messages[-1]["content"]).get("latest_comment", "")
            reply = '{"action": "chat"}'
            for marker, value in llm.decisions.items():
                if marker in latest:
                    reply = value
                    break
            yield reply

        llm.chat_completion = chat_completion


def stage_with_twinkle() -> RecordingStage:
    stage = RecordingStage()
    stage.start_session("mika")
    stage.decide(
        "twinkle",
        '{"action":"create_and_run","language":"python","subject":"twinkling stars"}',
    )
    stage.code(TWINKLE_PY)
    stage.say("make a twinkle animation in matplotlib")
    assert "Magical Twinkle" in stage.source
    # The viewer's earlier rules for that program.
    stage.lesson.update_project_state(
        requirements=["make the stars twinkle", "add wizards"]
    )
    return stage


def test_a_new_topic_writes_a_new_program_instead_of_editing_the_old_one():
    stage = stage_with_twinkle()
    stage.decide(
        "update the code",
        json.dumps(
            {
                "action": "modify",
                "language": "python",
                "subject": "Python lists basics",
                "fresh": True,
                "brief": "A Python list lesson: make a snacks list, append, remove and "
                "index items, and print each step.",
            }
        ),
    )
    stage.code(LISTS_PY)
    stage.say("yeah update the code")

    request = stage.generate_requests[-1]
    # Written from scratch: the twinkle program is not handed over to edit.
    assert "current_program" not in request
    assert "twinkle" not in json.dumps(request).lower()
    # The writer knows what "it" is.
    assert "snacks list" in request["what_to_build"]
    assert "yeah update the code" in request["what_to_build"]
    # The new program is on screen, the old rules and goal are gone.
    assert "snacks" in stage.source and "Twinkle" not in stage.source
    assert stage.lesson.project_requirements == []
    assert stage.session.teaching.session.goal == "Python lists basics"


def test_a_change_to_the_same_program_still_edits_it():
    stage = stage_with_twinkle()
    stage.decide(
        "more stars",
        json.dumps(
            {
                "action": "modify",
                "subject": "more stars",
                "fresh": False,
                "brief": "Double the number of stars in the twinkle picture.",
            }
        ),
    )
    stage.code(TWINKLE_PY.replace("60", "120"))
    stage.say("add more stars")
    request = stage.generate_requests[-1]
    assert "Magical Twinkle" in request["current_program"]
    assert "Double the number of stars" in request["what_to_build"]
    assert stage.lesson.project_requirements  # kept for the same program


def test_switching_language_is_always_a_new_program():
    decision = CodingDecision(action="modify", language="web", fresh=False)
    assert resolve_program_target(decision, "python") == ("web", True)
    # An omitted language keeps the program's language (it used to become Python).
    decision = CodingDecision(action="modify", language="", fresh=False)
    assert resolve_program_target(decision, "web") == ("web", False)
    assert resolve_program_target(CodingDecision(action="create"), "") == (
        "python",
        False,
    )


def test_the_decision_sees_what_the_program_is():
    stage = stage_with_twinkle()
    seen: list[str] = []
    original = stage.llm.chat_completion

    async def spy(messages, system=None, tools=None):
        blob = "\n".join(str(m.get("content") or "") for m in messages)
        if DECIDE_SYSTEM[:60] in blob:
            seen.append(messages[-1]["content"])
        async for chunk in original(messages, system=system, tools=tools):
            yield chunk

    stage.llm.chat_completion = spy
    stage.say("i want to learn lists now")
    situation = json.loads(seen[-1])["situation"]
    assert "Magical Twinkle" in situation["existing_program"]["preview"]


def test_parse_decision_reads_fresh_and_brief():
    d = parse_decision('{"action":"modify","fresh":true,"brief":"  Lists   lesson. "}')
    assert d.fresh is True and d.brief == "Lists lesson."
    # Not a code action: no brief, never fresh.
    d = parse_decision('{"action":"chat","fresh":true,"brief":"x"}')
    assert d.fresh is False and d.brief == ""
    # A string "true" from a sloppy model still counts.
    assert parse_decision('{"action":"create","fresh":"true"}').fresh is True


def test_create_and_a_different_template_always_start_fresh():
    # "make a snake game" while the shooter is on screen is a new program.
    assert resolve_program_target(
        CodingDecision(action="create", kind="web_game"), "web", "web_game_shooter"
    ) == ("web", True)
    assert resolve_program_target(
        CodingDecision(action="modify", kind="web_game", fresh=False),
        "web",
        "web_game_shooter",
    ) == ("web", True)
    # Same template: a change stays a change.
    assert resolve_program_target(
        CodingDecision(action="modify", kind="web_game_shooter", fresh=False),
        "web",
        "web_game_shooter",
    ) == ("web", False)
