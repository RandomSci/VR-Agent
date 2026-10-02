"""Character ownership: the character a viewer names is the one who answers.

The live bug: once a coding session was active, EVERY comment was reassigned
to the session owner, so "Mikaaaa How are you?" was answered by Luna.

These run the real path (viewer message -> room router -> ConversationDirector
-> speaking character) with the LLM replaced by stubs, because the routing and
ownership logic under test is deterministic and does not need a model.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from open_llm_vtuber.room.live_message import LiveMessage  # noqa: E402
from open_llm_vtuber.room.profiles import load_room  # noqa: E402
from open_llm_vtuber.room.session import RoomSession  # noqa: E402


class Room:
    """A room whose characters answer with a stub instead of an LLM."""

    def __init__(self, semantic=None):
        self.session = RoomSession(load_room(ROOT / "room", ROOT))
        self.spoke: list[tuple[str, str]] = []  # (character, turn kind)
        self.semantic = semantic or {}
        self.classifier_calls = 0

        async def turn_runner(turn, prompt, plan):
            self.spoke.append((turn.speaker, turn.kind))
            return f"{turn.speaker} speaks."

        async def classify(character_id, text, context):
            self.classifier_calls += 1
            return dict(self.semantic.get(text, {}))

        self.session.director.turn_runner = turn_runner
        self.session.director.teaching_intent_classifier = classify

    def say(self, text: str, user: str = "@selwyn") -> list[tuple[str, str]]:
        """One viewer comment, all the way through. Returns who spoke."""
        before = len(self.spoke)
        message = LiveMessage(
            platform="dev",
            message_id=f"t-{time.time_ns()}",
            username=user,
            text=text,
            timestamp=time.time(),
            author_id=f"dev:{user.lstrip('@')}",
        )

        async def go():
            if self.session.observe_viewer_message(message):
                return
            plan = self.session.director.plan(message)
            if plan.turns:
                await self.session.director.run(plan)

        asyncio.run(go())
        return self.spoke[before:]

    def speakers(self, text: str, user: str = "@selwyn") -> list[str]:
        return [who for who, _ in self.say(text, user)]

    def start_coding(self, teacher: str, user: str = "@selwyn") -> None:
        self.session.teaching.start(
            student_id=f"dev:{user.lstrip('@')}",
            student_name=user,
            goal="draw a heart",
            teacher=teacher,
        )
        self.session.teaching.set_phase("teaching")


# ---------------------------------------------------------------------------
# normal room, no coding session
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("mika how are you?", "mika"),
        ("Mika how are you?", "mika"),
        ("Mikaaaa How are you?", "mika"),  # chat stretches names
        ("MIKA!! hello", "mika"),
        ("@mika hi", "mika"),
        ("luna how are you?", "luna"),
        ("Luuuna hi", "luna"),
        ("Why is she answering for you mika", "mika"),
    ],
)
def test_named_character_answers_in_the_normal_room(text, expected):
    room = Room()
    assert room.speakers(text)[0] == expected


def test_unaddressed_chat_still_works():
    room = Room()
    assert room.speakers("hello everyone")


# ---------------------------------------------------------------------------
# with a coding session active: the regression
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("owner,other", [("luna", "mika"), ("mika", "luna")])
def test_a_coding_session_never_steals_a_named_message(owner, other):
    room = Room()
    room.start_coding(owner)
    assert room.session.teaching.session.active

    # The other character is named: she answers, not the session owner.
    assert room.speakers(f"{other} how are you?")[0] == other
    assert room.speakers(f"{other.capitalize()}aaa How are you?")[0] == other

    # The owner is named: the owner answers.
    assert room.speakers(f"{owner} how are you?")[0] == owner

    # Nobody named: the session owner answers, because that is whose
    # work the room is watching.
    assert room.speakers("how is it going?")[0] == owner

    # The session itself is untouched by any of that.
    assert room.session.teaching.session.active
    assert room.session.teaching.session.teacher == owner


def test_the_card_target_follows_the_real_speaker():
    """TO MIKA / TO LUNA is backend ownership, not decoration."""
    room = Room()
    room.start_coding("luna")
    message = LiveMessage(
        platform="dev",
        message_id="t-card",
        username="@selwyn",
        text="mika how are you?",
        timestamp=time.time(),
        author_id="dev:selwyn",
    )
    plan = room.session.director.plan(message)
    asyncio.run(room.session.director.run(plan))
    assert plan.turns[0].speaker == "mika"
    assert room.spoke[0][0] == "mika"


def test_addressed_answer_is_an_ordinary_turn_not_a_lesson_turn():
    """Answering a different character must not be framed as teaching."""
    room = Room()
    room.start_coding("luna")
    kinds = dict(room.say("mika how are you?"))
    assert kinds["mika"] == "answer"
    kinds = dict(room.say("luna how are you?"))
    assert kinds["luna"].startswith("teaching_")


# ---------------------------------------------------------------------------
# handing the live coding work over
# ---------------------------------------------------------------------------


def test_naming_a_character_does_not_hand_over_the_coding_work():
    """Saying "mika how are you?" must not make Mika the one coding."""
    room = Room()
    room.start_coding("luna")
    room.say("mika how are you?")
    assert room.session.teaching.session.teacher == "luna"
    room.say("mika what do you think of this code?")
    assert room.session.teaching.session.teacher == "luna"


def test_an_explicit_switch_hands_the_work_over():
    text = "let mika take over the coding"
    room = Room(semantic={text: {"intent": "switch_teacher", "teacher": "mika"}})
    room.start_coding("luna")
    room.say(text)
    assert room.session.teaching.session.teacher == "mika"


def test_only_the_session_owner_may_hand_the_work_over():
    text = "let mika take over the coding"
    room = Room(semantic={text: {"intent": "switch_teacher", "teacher": "mika"}})
    room.start_coding("luna", user="@selwyn")
    room.say(text, user="@someone_else")
    assert room.session.teaching.session.teacher == "luna"
