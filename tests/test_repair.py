"""Check, then repair once: never an endless loop, never a silent edit.

* A web program that fails the browser check gets ONE repair with the real
  problems; the repaired version is shown and the character may mention it.
* A repair that makes things worse is thrown away.
* A Python program written in this job that crashes gets ONE repair from its
  real traceback and runs again.
* A plain "run" never edits the source, even when it fails.
* VR_CODE_REPAIRS=0 turns repairs off.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from open_llm_vtuber.room.browser_qa import BrowserReport  # noqa: E402
from tests.test_code_in_public import HEART_WEB  # noqa: E402
from tests.test_topic_switch import RecordingStage  # noqa: E402

BROKEN_WEB = HEART_WEB.replace("</body>", "<script>undefinedThing.go()</script></body>")
FIXED_WEB = HEART_WEB.replace("COLOUR", "gold")


class SequenceLLM:
    """Code replies in order: first write, then the repair."""

    def __init__(self, stage: RecordingStage, replies: list[str]):
        self.replies = list(replies)
        stage.code(self.replies[0])
        original = stage.llm.chat_completion

        async def chat(messages, system=None, tools=None):
            blob = "\n".join(str(m.get("content") or "") for m in messages)
            if '"what_to_build"' in blob and self.replies:
                stage.llm.code_reply = self.replies.pop(0)
            async for chunk in original(messages, system=system, tools=tools):
                yield chunk

        stage.llm.chat_completion = chat


def checker(*reports: BrowserReport):
    calls: list[str] = []
    queue = list(reports)

    async def check(source: str) -> BrowserReport:
        calls.append(source)
        return queue.pop(0) if queue else BrowserReport(ok=True)

    check.calls = calls  # type: ignore[attr-defined]
    return check


def broken_report() -> BrowserReport:
    return BrowserReport(
        ok=False, errors=["ReferenceError: undefinedThing is not defined"]
    )


def web_stage(*replies: str) -> RecordingStage:
    stage = RecordingStage()
    stage.start_session("mika")
    SequenceLLM(stage, list(replies))
    stage.decide(
        "heart page",
        json.dumps(
            {"action": "create", "kind": "web_canvas", "subject": "a beating heart"}
        ),
    )
    return stage


def test_a_broken_web_program_is_repaired_once():
    stage = web_stage(BROKEN_WEB, FIXED_WEB)
    check = checker(broken_report(), BrowserReport(ok=True))
    stage.runtimes.browser_check = check
    stage.say("make a heart page")

    assert len(check.calls) == 2, "checked, repaired, checked again: no more"
    repair_request = stage.generate_requests[-1]
    assert "undefinedThing" in repair_request["fix_these_problems"]
    assert "undefinedThing" in repair_request["current_program"]
    assert stage.source.strip() == FIXED_WEB.strip()
    phases = [o.get("phase") for o in stage.ops if o.get("op") == "coding"]
    assert "checking" in phases and "fixing" in phases
    assert stage.lesson.last_check["ok"] is True
    # Mika is told about the caught bug, and only what really happened.
    assert "caught a problem" in stage.last_prompt()
    assert "no errors, it draws and it moves" in stage.last_prompt()


def test_a_repair_that_makes_it_worse_is_thrown_away():
    stage = web_stage(BROKEN_WEB, "<html><body>worse</body></html>")
    worse = BrowserReport(ok=False, errors=["a", "b"], blank=True)
    stage.runtimes.browser_check = checker(broken_report(), worse)
    stage.say("make a heart page")
    assert stage.source.strip() == BROKEN_WEB.strip()
    # Still broken, so Mika must not claim it works.
    assert "Do not claim it works" in stage.last_prompt()


def test_a_passing_program_is_not_touched():
    stage = web_stage(FIXED_WEB, "SHOULD NEVER BE USED")
    check = checker(BrowserReport(ok=True))
    stage.runtimes.browser_check = check
    stage.say("make a heart page")
    assert len(check.calls) == 1
    assert len(stage.generate_requests) == 1
    assert stage.source.strip() == FIXED_WEB.strip()


def test_repairs_can_be_switched_off():
    stage = web_stage(BROKEN_WEB, FIXED_WEB)
    stage.runtimes.max_repairs = 0
    check = checker(broken_report())
    stage.runtimes.browser_check = check
    stage.say("make a heart page")
    assert len(check.calls) == 1 and stage.source.strip() == BROKEN_WEB.strip()


def _coding_python_ok() -> bool:
    python = os.environ.get("VR_CODING_PYTHON") or sys.executable
    return (
        subprocess.run([python, "-c", "print(1)"], capture_output=True).returncode == 0
    )


@pytest.mark.skipif(not _coding_python_ok(), reason="no coding Python")
def test_a_crashing_python_program_is_repaired_from_its_traceback(monkeypatch):
    from tests.test_stage_live import FAKE_BWRAP  # the bubblewrap stand-in

    import shutil

    if not shutil.which("bwrap"):
        tool = Path(os.environ.get("TMPDIR", "/tmp")) / "vr-test-bwrap" / "bwrap"
        tool.parent.mkdir(parents=True, exist_ok=True)
        tool.write_text(FAKE_BWRAP.format(python=sys.executable))
        tool.chmod(0o755)
        monkeypatch.setenv("PATH", f"{tool.parent}{os.pathsep}{os.environ['PATH']}")
    stage = RecordingStage()
    stage.start_session("mika")
    SequenceLLM(stage, ["print(snaks)\n", "snacks = ['chips']\nprint(snacks)\n"])
    stage.decide(
        "lists",
        json.dumps(
            {
                "action": "create_and_run",
                "kind": "python_lesson",
                "subject": "Python lists",
            }
        ),
    )
    stage.say("teach me lists and run it")

    assert "NameError" in stage.generate_requests[-1]["fix_these_problems"]
    assert stage.lesson.last_result.exit_code == 0
    assert "chips" in stage.lesson.last_result.stdout
    assert "fixed the code and ran it again" in stage.last_prompt()


def test_a_plain_run_never_edits_the_source():
    stage = RecordingStage()
    stage.start_session("mika")
    stage.lesson.set_artifact("dev:selwyn", "python", "print(snaks)\n")
    before = stage.source
    stage.decide("run it", '{"action": "run"}')
    stage.say("run it")
    assert stage.source == before
    assert stage.generate_requests == []
