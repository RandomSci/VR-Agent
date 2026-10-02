"""Faces and short lines while coding, and lines that cannot deadlock.

* A real bug makes the character pout (her own face, from her registry); a
  working repair gives sparkly eyes; a repair that fails gives a sad face.
* Writing that takes more than a couple of seconds shows a focused face.
* Lines never repeat back to back.
* A line spoken from inside a viewer interaction never waits for the
  interaction's own speech lock (the live handler holds it for the whole
  interaction; waiting would freeze the stream).
"""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from open_llm_vtuber.room.browser_qa import BrowserReport  # noqa: E402
from open_llm_vtuber.room.coding_mood import LinePicker, face_for  # noqa: E402
from open_llm_vtuber.room.speech import INSIDE_INTERACTION  # noqa: E402
from tests.test_repair import (  # noqa: E402
    BROKEN_WEB,
    FIXED_WEB,
    broken_report,
    checker,
)
from tests.test_repair import web_stage as _web_stage  # noqa: E402

MIKA_FACES = {"pout", "sparkle", "sad", "close_eyes", "smile", "surprised"}


def web_stage(*replies):
    """Mika with her real face names (the model files are not needed here)."""
    stage = _web_stage(*replies)
    from open_llm_vtuber.vr_agent.capabilities import (
        CharacterAction,
        CharacterCapabilities,
    )

    caps = CharacterCapabilities(model_name="mao_pro")
    for name in MIKA_FACES:
        caps.actions[name] = CharacterAction(
            name=name,
            kind="expression",
            label=name,
            description=name,
            expression_name=name,
            hold_seconds=3.0,
        )
    stage.session.room.get("mika").capabilities = caps
    return stage


def faces(stage, character="mika"):
    return [
        o["name"]
        for o in stage.ops
        if o.get("op") == "action" and o.get("character") == character
    ]


def mika_supports(name: str) -> bool:
    return name in {"pout", "sparkle", "sad", "close_eyes", "smile"}


def test_faces_come_from_what_the_model_has():
    assert face_for("bug", mika_supports) == "pout"
    assert face_for("fixed", mika_supports) == "sparkle"
    assert face_for("focus", lambda n: n == "head_tilt") == "head_tilt"
    assert face_for("bug", lambda n: False) == ""


def test_lines_do_not_repeat_back_to_back():
    picker = LinePicker(random.Random(3))
    lines = [picker.line("bug", "mika") for _ in range(30)]
    assert all(a != b for a, b in zip(lines, lines[1:]))
    assert picker.line("bug", "luna") != ""
    assert picker.line("bug", "someone-new")  # falls back to Mika's lines


def test_a_bug_pouts_and_a_working_fix_sparkles():
    stage = web_stage(BROKEN_WEB, FIXED_WEB)
    stage.runtimes.browser_check = checker(broken_report(), BrowserReport(ok=True))
    stage.say("make a heart page")
    seen = faces(stage)
    assert "pout" in seen and "sparkle" in seen
    assert seen.index("pout") < seen.index("sparkle")


def test_a_failed_fix_is_a_sad_face_not_a_celebration():
    stage = web_stage(BROKEN_WEB, "<html><body>worse</body></html>")
    worse = BrowserReport(ok=False, errors=["a", "b"], blank=True)
    stage.runtimes.browser_check = checker(broken_report(), worse)
    stage.say("make a heart page")
    seen = faces(stage)
    assert "pout" in seen and "sad" in seen and "sparkle" not in seen


def test_long_writing_shows_a_focused_face():
    stage = web_stage(FIXED_WEB)
    stage.runtimes.browser_check = checker(BrowserReport(ok=True))
    stage.runtimes.mood_timing = (0.05, 99.0, 0.05)
    original = stage.runtimes.generate_code

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.3)
        return await original(*args, **kwargs)

    stage.runtimes.generate_code = slow
    stage.say("make a heart page")
    assert "close_eyes" in faces(stage)


def test_a_line_inside_an_interaction_does_not_wait_for_its_lock():
    stage = web_stage(FIXED_WEB)
    speech = stage.session.speech

    async def scenario():
        await speech.lock.acquire()  # what the live handler does
        try:

            async def inside():
                INSIDE_INTERACTION.set(True)
                async with speech._turn():
                    return "spoke"

            assert await asyncio.wait_for(asyncio.create_task(inside()), 1) == "spoke"

            async def outside():
                async with speech._turn():
                    return "spoke"

            try:
                await asyncio.wait_for(outside(), 0.2)
                return False  # outside lines must wait for the interaction
            except asyncio.TimeoutError:
                return True
        finally:
            speech.lock.release()

    assert asyncio.run(scenario())
