"""Code in Public, end to end through the real path.

A viewer comment goes: room router -> ConversationDirector -> one semantic
decision -> the coding worker -> generate / write / run -> the character
speaks about the verified result.

The LLM is scripted so the test is deterministic, but everything else is the
real code: the real decision parser, the real worker, the real lesson state
and the real sandboxed runner (so matplotlib really executes).
"""

from __future__ import annotations

import asyncio
import hashlib
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from open_llm_vtuber.room.coding_actions import DECIDE_SYSTEM  # noqa: E402
from open_llm_vtuber.room.live_message import LiveMessage  # noqa: E402
from open_llm_vtuber.room.profiles import load_room  # noqa: E402
from open_llm_vtuber.room.runtime import RoomRuntimes  # noqa: E402
from open_llm_vtuber.room.session import RoomSession  # noqa: E402

HEART_PY = """import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

t = np.linspace(0, 2 * np.pi, 400)
x = 16 * np.sin(t) ** 3
y = 13 * np.cos(t) - 5 * np.cos(2 * t) - 2 * np.cos(3 * t) - np.cos(4 * t)
plt.fill(x, y, color="%s")
plt.axis("equal")
plt.axis("off")
plt.savefig("output.png", dpi=90)
"""

HEART_WEB = """<!doctype html>
<html><head><meta charset="utf-8"><style>
body { background: #111; display: grid; place-items: center; height: 100vh; }
.heart { width: 120px; height: 120px; background: COLOUR; animation: beat 1s infinite; }
@keyframes beat { 50% { transform: scale(1.15); } }
</style></head><body><div class="heart"></div></body></html>
"""

BROKEN_PY = "print(unknown_name_that_does_not_exist)\n"


class ScriptLLM:
    """Answers the decision call and the code call from a script."""

    def __init__(self):
        self.decisions: dict[str, str] = {}
        self.code_reply = ""
        self.decide_calls = 0
        self.generate_calls = 0

    async def chat_completion(self, messages, system=None, tools=None):
        blob = "\n".join(str(m.get("content") or "") for m in messages)
        if DECIDE_SYSTEM[:60] in blob:
            self.decide_calls += 1
            reply = '{"action": "chat"}'
            for marker, value in self.decisions.items():
                if marker in blob:
                    reply = value
                    break
        else:
            self.generate_calls += 1
            reply = self.code_reply
        for i in range(0, len(reply), 64):
            yield reply[i : i + 64]


class Stage:
    """A room with scripted characters and the real coding pipeline."""

    def __init__(self):
        self.session = RoomSession(load_room(ROOT / "room", ROOT))
        self.llm = ScriptLLM()
        self.said: list[tuple[str, str]] = []
        self.ops: list[dict] = []

        agent = type("Agent", (), {"_llm": self.llm})()
        runtimes = RoomRuntimes(self.session, None, "test", self._send)
        runtimes.agent = lambda character_id: agent
        self.runtimes = runtimes

        async def turn_runner(turn, prompt, plan):
            self.said.append((turn.speaker, prompt))
            return f"{turn.speaker} says something."

        async def push(ops):
            self.ops.extend(o for o in (ops if isinstance(ops, list) else [ops]) if o)

        self.session.push = push
        self.session.director.turn_runner = turn_runner
        self.session.director.teaching_intent_classifier = runtimes.classify_teaching_intent
        self.session.director.coding_action_runner = runtimes.run_coding_action

    async def _send(self, payload: str) -> None:
        pass

    # -- driving --------------------------------------------------------
    def decide(self, marker: str, json_reply: str) -> "Stage":
        self.llm.decisions[marker] = json_reply
        return self

    def code(self, text: str) -> "Stage":
        self.llm.code_reply = text
        return self

    def say(self, text: str, user: str = "@selwyn") -> None:
        message = LiveMessage(
            platform="dev",
            message_id=f"c-{time.time_ns()}",
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

    def start_session(self, teacher: str = "luna", user: str = "@selwyn") -> None:
        self.session.teaching.start(
            student_id=f"dev:{user.lstrip('@')}",
            student_name=user,
            goal="code in public",
            teacher=teacher,
        )
        self.session.teaching.set_phase("teaching")

    # -- inspecting -----------------------------------------------------
    @property
    def lesson(self):
        return self.session.teaching.coding_lesson

    @property
    def source(self) -> str:
        return self.lesson.code if self.lesson else ""

    def source_hash(self) -> str:
        return hashlib.sha256(self.source.encode()).hexdigest()

    def coding_ops(self, phase: str) -> list[dict]:
        return [o for o in self.ops if o.get("op") == "coding" and o.get("phase") == phase]

    def last_prompt(self) -> str:
        return self.said[-1][1] if self.said else ""


# ---------------------------------------------------------------------------
# the action model
# ---------------------------------------------------------------------------


def test_a_direct_request_creates_code_with_no_second_confirmation():
    stage = Stage()
    stage.start_session("luna")
    stage.decide("matplotlib heart", '{"action":"create","language":"python"}')
    stage.code(HEART_PY % "red")
    stage.say("create a matplotlib heart")
    assert "matplotlib" in stage.source
    assert stage.coding_ops("writing"), "the source never reached the Stage"


@pytest.mark.parametrize(
    "comment",
    [
        "make the heart green",  # a command
        "can you make the heart green?",  # a question
        "pwede bang gawing green yung heart?",  # another language entirely
    ],
)
def test_a_request_is_already_authorization(comment):
    """No waiting for "okay" or "do it"."""
    stage = Stage()
    stage.start_session("luna")
    stage.lesson.set_artifact("dev:selwyn", "python", HEART_PY % "red")
    stage.decide(comment, '{"action":"modify","language":"python"}')
    stage.code(HEART_PY % "green")
    stage.say(comment)
    assert "green" in stage.source


def test_modify_and_run_does_both():
    stage = Stage()
    stage.start_session("luna")
    stage.lesson.set_artifact("dev:selwyn", "python", HEART_PY % "red")
    stage.decide("blue and run", '{"action":"modify_and_run","language":"python"}')
    stage.code(HEART_PY % "blue")
    stage.say("make it blue and run it")
    assert "blue" in stage.source
    result = stage.lesson.last_result
    assert result is not None and result.exit_code == 0, "it never actually ran"


def test_run_executes_the_existing_source_byte_for_byte():
    stage = Stage()
    stage.start_session("luna")
    stage.lesson.set_artifact("dev:selwyn", "python", HEART_PY % "red")
    before = stage.source_hash()
    stage.decide("run it", '{"action":"run"}')
    stage.code("THIS MUST NEVER BE WRITTEN")
    stage.say("run it")
    assert stage.source_hash() == before, "run rewrote the source"
    assert stage.llm.generate_calls == 0, "run called the code generator"
    assert stage.lesson.last_result is not None


def test_ordinary_chat_never_touches_the_source():
    stage = Stage()
    stage.start_session("luna")
    stage.lesson.set_artifact("dev:selwyn", "python", HEART_PY % "red")
    before = stage.source_hash()
    stage.say("this looks so cool")  # decides to chat by default
    stage.say("luna how are you?")
    assert stage.source_hash() == before
    assert stage.llm.generate_calls == 0


# ---------------------------------------------------------------------------
# results the characters can actually see
# ---------------------------------------------------------------------------


def test_matplotlib_produces_a_real_image():
    stage = Stage()
    stage.start_session("luna")
    stage.decide("heart", '{"action":"create_and_run","language":"python"}')
    stage.code(HEART_PY % "crimson")
    stage.say("draw me a heart")
    result = stage.lesson.last_result
    assert result is not None and result.exit_code == 0, getattr(result, "error", "no result")
    artifacts = stage.runtimes._coding_runner.artifacts(stage.lesson)
    assert any(a.get("name", "").endswith(".png") for a in artifacts), artifacts


def test_a_web_project_is_previewed_not_executed():
    stage = Stage()
    stage.start_session("mika")
    stage.decide("animated heart", '{"action":"create","language":"web"}')
    stage.code(HEART_WEB.replace("COLOUR", "hotpink"))
    stage.say("make an animated heart")
    assert stage.lesson.language == "web"
    assert "<!doctype html>" in stage.source.lower()
    ready = [o for o in stage.ops if o.get("op") == "coding" and o.get("preview_ready")]
    assert ready, "the browser preview was never offered"


def test_a_real_error_reaches_the_character():
    stage = Stage()
    stage.start_session("luna")
    stage.decide("broken", '{"action":"create_and_run","language":"python"}')
    stage.code(BROKEN_PY)
    stage.say("run this broken thing")
    result = stage.lesson.last_result
    assert result is not None and result.exit_code != 0
    prompt = stage.last_prompt()
    assert "NameError" in prompt or "error" in prompt.lower(), prompt
    assert "unknown_name_that_does_not_exist" in prompt or "NameError" in prompt


def test_the_character_is_not_told_it_succeeded_when_it_failed():
    stage = Stage()
    stage.start_session("luna")
    stage.decide("broken", '{"action":"create_and_run","language":"python"}')
    stage.code(BROKEN_PY)
    stage.say("run this broken thing")
    prompt = stage.last_prompt().lower()
    assert "do not claim" in prompt or "failed" in prompt


# ---------------------------------------------------------------------------
# unsupported technology
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("technology", ["turtle", "manim", "tkinter"])
def test_unsupported_technology_is_redirected_not_attempted(technology):
    stage = Stage()
    stage.start_session("luna")
    stage.decide(
        technology,
        '{"action":"create","language":"python","unsupported":"%s"}' % technology,
    )
    stage.code("import %s  # must never run" % technology)
    stage.say(f"draw a square with {technology}")
    assert technology not in stage.source
    prompt = stage.last_prompt().lower()
    assert "headless" in prompt and technology in prompt, prompt
    assert stage.llm.generate_calls == 0, "it tried to write unsupported code"


# ---------------------------------------------------------------------------
# concurrency through the real director
# ---------------------------------------------------------------------------


def test_the_newest_change_wins_and_run_waits_for_it():
    """purple, then blue, then "run it when you're done" -> blue runs."""
    stage = Stage()
    stage.start_session("luna")
    stage.lesson.set_artifact("dev:selwyn", "python", HEART_PY % "red")
    worker = stage.runtimes.coding_worker

    async def go():
        jobs = []
        from open_llm_vtuber.room.coding_worker import CodingJob

        async def slow_generate(character_id, language, instruction, existing="", notes=None, kind=""):
            await asyncio.sleep(0.05)
            return HEART_PY % instruction

        stage.runtimes.generate_code = slow_generate
        for colour in ("purple", "blue"):
            jobs.append(
                worker.enqueue(
                    CodingJob(
                        action="modify",
                        owner_id="dev:selwyn",
                        language="python",
                        instruction=colour,
                        speaker="luna",
                    )
                )
            )
        run = worker.enqueue(
            CodingJob(action="run", owner_id="dev:selwyn", language="python", speaker="luna")
        )
        await worker.drain()
        return [await j for j in jobs], await run

    results, run = asyncio.run(go())
    assert "blue" in stage.source and "purple" not in stage.source
    assert run.get("attached") is True
    assert stage.lesson.last_result is not None


# ---------------------------------------------------------------------------
# starting a session, and who owns it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["luna", "mika"])
def test_let_us_code_starts_a_session_owned_by_the_character_asked(who):
    stage = Stage()
    stage.decide("let's code", '{"action":"start_coding"}')
    stage.say(f"{who} let's code")
    session = stage.session.teaching.session
    assert session.active, "no coding session started"
    assert session.teacher == who


def test_an_animation_request_prefers_the_web():
    """Anything that moves should be a browser project, not matplotlib."""
    stage = Stage()
    stage.start_session("mika")
    stage.decide("bouncing ball", '{"action":"create","language":"web"}')
    stage.code(HEART_WEB.replace("COLOUR", "aqua"))
    stage.say("make a bouncing ball animation")
    assert stage.lesson.language == "web"


def test_the_source_survives_ordinary_chat_between_changes():
    stage = Stage()
    stage.start_session("luna")
    stage.decide("heart", '{"action":"create","language":"python"}')
    stage.code(HEART_PY % "red")
    stage.say("draw a heart")
    first = stage.source_hash()
    stage.say("that's so pretty")
    stage.say("mika what do you think?")
    assert stage.source_hash() == first
