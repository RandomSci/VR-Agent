"""An LLM failure must never be spoken or shown on stream.

Regression from DEV: a failed OpenAI call made Mika say "Error calling the
chat endpoint: Error occurred while generating response." in her subtitle.
Open-LLM-VTuber turns its own error message into a normal spoken sentence.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from open_llm_vtuber.room import runtime as rt  # noqa: E402
from tests.test_code_in_public import Stage  # noqa: E402

ERROR = "Error calling the chat endpoint: Error occurred while generating response. See the logs for details."


def audio(text: str) -> str:
    return json.dumps(
        {"type": "audio", "audio": "UklGRg==", "display_text": {"text": text}}
    )


def run(monkeypatch, lines: list[str], returned: str):
    import open_llm_vtuber.conversations.single_conversation as sc

    async def fake(context, websocket_send, **kwargs):
        for line in lines:
            await websocket_send(audio(line))
        return returned

    monkeypatch.setattr(sc, "process_single_conversation", fake)
    stage = Stage()
    sent: list[str] = []

    async def send(payload):
        sent.append(payload)

    runtimes = stage.runtimes
    runtimes.send = send
    runtimes._live2d = lambda cid: None
    stage.session.voices.engine = lambda cid: None
    turn = SimpleNamespace(
        speaker="mika", addressee="", kind="chat", intent=SimpleNamespace(action=None)
    )
    plan = SimpleNamespace(id="i1", timing=None, message=SimpleNamespace(timestamp=0))
    text = asyncio.run(runtimes.run_turn(turn, "hi", plan))
    return text, sent


def test_the_error_is_not_spoken_or_returned(monkeypatch):
    text, sent = run(monkeypatch, [ERROR], ERROR)
    assert text == ""
    assert not any("Error calling the chat endpoint" in p for p in sent)


def test_normal_lines_still_go_out(monkeypatch):
    text, sent = run(monkeypatch, ["Hi there!"], "Hi there!")
    assert text == "Hi there!"
    assert any("Hi there!" in p for p in sent)


def test_detection():
    assert rt.is_llm_error_text("  " + ERROR)
    assert not rt.is_llm_error_text("I made an error in that loop, let me fix it.")
    assert not rt.is_llm_error_text(None)
