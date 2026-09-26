"""Headless browser test of the room page against the harness server.

Proves in a real browser that both Live2D characters load independently,
the renderer detects look parameters per model, attention ops turn heads,
audio plays on the right character and the page reports playback complete,
and a disabled room falls back to the classic livestream page.

Skipped when no Chromium is available. Set VR_TEST_CHROMIUM to a Chromium
binary to use a specific one.
"""

from __future__ import annotations

import base64
import io
import os
import shutil
import socket
import threading
import time
import wave
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")
httpx = pytest.importorskip("httpx")

from tests.harness.room_server import create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def chromium_path() -> str | None:
    env = os.environ.get("VR_TEST_CHROMIUM")
    if env and Path(env).exists():
        return env
    for candidate in sorted(
        Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome")
    ):
        return str(candidate)
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, room_dir: Path | None = None):
        self.port = free_port()
        self.app = create_app(room_dir)
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port, log_level="warning"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 20
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def post(self, path: str, body: dict) -> None:
        httpx.post(self.base + path, json=body, timeout=10).raise_for_status()


def tone_payload(character: str) -> dict:
    rate = 16000
    frames = b"".join(
        int(8000 * ((i // 400) % 2)).to_bytes(2, "little", signed=True)
        for i in range(rate)
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return {
        "type": "audio",
        "audio": base64.b64encode(buf.getvalue()).decode(),
        "volumes": [1.0 if (i // 1) % 2 else 0.2 for i in range(50)],
        "slice_length": 20,
        "display_text": {"text": "Testing one two.", "name": character},
        "actions": {"expressions": [1]},
        "character": character,
        "emotion_mode": "profile",
    }


@pytest.fixture(scope="module")
def browser():
    path = chromium_path()
    if not path:
        pytest.skip("no Chromium available")
    with playwright_api.sync_playwright() as p:
        b = p.chromium.launch(
            executable_path=path,
            args=[
                "--use-gl=angle",
                "--use-angle=swiftshader",
                "--enable-unsafe-swiftshader",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        yield b
        b.close()


def open_room(browser, server: Server):
    page = browser.new_page(viewport={"width": 960, "height": 540})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(server.base + "/vr-agent/room.html")
    page.wait_for_function(
        "window.vrRoom && vrRoom.state().characters.length === 2 && "
        "vrRoom.state().characters.every(c => c.loaded || c.failed)",
        timeout=120000,
    )
    return page, errors


def test_two_characters_render_look_at_each_other_and_speak(browser):
    with Server() as server:
        page, errors = open_room(browser, server)
        state = page.evaluate("vrRoom.state()")
        assert [c["id"] for c in state["characters"]] == ["mika", "luna"]
        for c in state["characters"]:
            assert c["loaded"] and c["look"] == {
                "head": True,
                "eyes": True,
                "body": True,
            }
        assert state["characters"][0]["mouth"] == "ParamA"
        assert state["characters"][1]["mouth"] == "ParamMouthOpenY"

        # The server learns which models loaded.
        status = httpx.get(server.base + "/harness/clients").json()
        assert list(status["clients"].values())[0]["loaded"] == ["mika", "luna"]

        # Attention: Mika (left) looks right at Luna, Luna looks left at Mika.
        server.post(
            "/harness/push",
            {
                "ops": [
                    {
                        "op": "attention",
                        "character": "mika",
                        "target": "CHARACTER:luna",
                        "hold_ms": 5000,
                    },
                    {
                        "op": "attention",
                        "character": "luna",
                        "target": "CHARACTER:mika",
                        "hold_ms": 5000,
                    },
                ]
            },
        )
        page.wait_for_function(
            "vrRoom.state().characters[0].focus.x > 0.5 && vrRoom.state().characters[1].focus.x < -0.5",
            timeout=5000,
        )
        # Both look down toward the game board.
        server.post(
            "/harness/push",
            {
                "ops": [
                    {
                        "op": "attention",
                        "character": "mika",
                        "target": "GAME",
                        "hold_ms": 5000,
                    },
                    {
                        "op": "attention",
                        "character": "luna",
                        "target": "OBJECT:game_board",
                        "hold_ms": 5000,
                    },
                ]
            },
        )
        page.wait_for_function(
            "vrRoom.state().characters.every(c => c.focus.y < -0.3)", timeout=5000
        )
        # A summoned rabbit becomes a room object both characters look at
        # once it appears during the motion.
        httpx.post(
            server.base + "/harness/play",
            json={"character": "mika", "action": "summon_rabbit"},
        ).raise_for_status()
        page.wait_for_function(
            "vrRoom.state().characters.every(c => c.target === 'OBJECT:rabbit')",
            timeout=15000,
        )
        # Invalid ops are ignored without errors.
        server.post(
            "/harness/push",
            {
                "ops": [
                    {
                        "op": "attention",
                        "character": "mika",
                        "target": "javascript:alert(1)",
                    }
                ]
            },
        )

        # Speech on Luna: Luna speaks, Mika looks at her, playback is reported.
        server.post(
            "/harness/push",
            {
                "ops": [
                    {
                        "op": "attention",
                        "character": "mika",
                        "target": "VIEWER",
                        "hold_ms": 500,
                    }
                ]
            },
        )
        time.sleep(0.8)
        server.post("/harness/send", tone_payload("luna"))
        server.post("/harness/send", {"type": "backend-synth-complete"})
        page.wait_for_function("vrRoom.state().characters[1].speaking", timeout=15000)
        assert page.evaluate("vrRoom.state().characters[0].speaking") is False
        page.wait_for_function(
            "vrRoom.state().characters[0].target === 'CHARACTER:luna'", timeout=5000
        )
        deadline = time.time() + 20
        kinds: list[str] = []
        while time.time() < deadline:
            kinds = [
                m["type"] for m in httpx.get(server.base + "/harness/received").json()
            ]
            if "frontend-playback-complete" in kinds:
                break
            time.sleep(0.2)
        assert "audio-play-start" in kinds and "frontend-playback-complete" in kinds
        assert errors == []
        page.close()


def test_disabled_room_falls_back_to_classic_page(browser, tmp_path):
    room_dir = tmp_path / "room"
    shutil.copytree(ROOT / "room", room_dir)
    text = (
        (room_dir / "room.yaml").read_text().replace("enabled: true", "enabled: false")
    )
    (room_dir / "room.yaml").write_text(text)
    with Server(room_dir) as server:
        page = browser.new_page()
        page.goto(server.base + "/vr-agent/room.html?subtitles=0")
        page.wait_for_url("**/?mode=live&subtitles=0", timeout=20000)
        page.close()
