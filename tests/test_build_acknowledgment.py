"""Mika speaks the moment a build starts, not after the code is written.

Writing code takes seconds; the stream must not go silent meanwhile. The
acknowledgment is a template line (no LLM call) spoken in parallel with code
generation, and the Stage shows "writing the code" until the source lands.
"""

from __future__ import annotations

import asyncio
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from open_llm_vtuber.room.acknowledge import acknowledgment, speakable_name  # noqa: E402
from tests.test_code_in_public import HEART_WEB, Stage  # noqa: E402


def test_mika_speaks_before_the_code_is_finished():
    stage = Stage()
    stage.start_session("mika")
    events: list[tuple[str, float]] = []

    # Code writing takes a while, like the real model.
    original = stage.llm.chat_completion

    async def slow(messages, system=None, tools=None):
        blob = "\n".join(str(m.get("content") or "") for m in messages)
        if '"what_to_build"' in blob:
            events.append(("generation_started", time.perf_counter()))
            await asyncio.sleep(0.6)
        async for chunk in original(messages, system=system, tools=tools):
            yield chunk
        if '"what_to_build"' in blob:
            events.append(("generation_finished", time.perf_counter()))

    stage.llm.chat_completion = slow

    async def say(character_id, text, addressee=None):
        events.append((f"said:{character_id}:{text}", time.perf_counter()))
        return True

    stage.session.speech.say = say
    stage.session.speech_allowed = lambda now=None: True

    stage.decide(
        "space game",
        '{"action":"create","language":"web","subject":"an asteroid game"}',
    )
    stage.code(HEART_WEB.replace("COLOUR", "gold"))
    stage.say("make a cool space game")

    names = [name for name, _ in events]
    said = [i for i, n in enumerate(names) if n.startswith("said:mika:")]
    assert said, f"Mika never acknowledged the build: {names}"
    assert said[0] < names.index("generation_finished"), names
    assert "an asteroid game" in names[said[0]]

    # The Stage showed the build before the source arrived.
    phases = [o.get("phase") for o in stage.ops if o.get("op") == "coding"]
    assert phases.index("building") < phases.index("writing"), phases
    # The explanation turn still happened after the build.
    assert stage.said and stage.said[-1][0] == "mika"


def test_no_acknowledgment_for_plain_chat():
    stage = Stage()
    stage.start_session("mika")
    spoken = []

    async def say(character_id, text, addressee=None):
        spoken.append(text)
        return True

    stage.session.speech.say = say
    stage.session.speech_allowed = lambda now=None: True
    stage.say("how are you mika?")
    assert spoken == []
    assert not [o for o in stage.ops if o.get("phase") == "building"]


def test_lines_are_speakable():
    rng = random.Random(1)
    line = acknowledgment("luna", "@Kuya_Jun!!<3", "a *cat* asteroid game!!", True, rng)
    assert "@" not in line and "*" not in line and "<" not in line
    assert "Kuya_Jun" in line and "cat asteroid game" in line
    assert speakable_name("") == "friend"
    assert "it" in acknowledgment("mika", "x", "", True, rng)
