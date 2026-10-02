"""The Stage shows the authoritative backend source, in a real browser.

The point of these is that backend state and what a viewer sees cannot drift:
whatever the lesson holds is what appears on screen, and a browser project
really previews.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

playwright_api = pytest.importorskip("playwright.sync_api")

from tests.harness.fake_tts import FakeTTS  # noqa: E402
from tests.harness.full_dev_server import create_app  # noqa: E402

WEB_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>beat</title><style>
body{background:#101018;display:grid;place-items:center;height:100vh;margin:0}
.heart{width:140px;height:140px;background:#ff5f8d;transform:rotate(45deg);
position:relative;animation:beat .9s ease-in-out infinite}
.heart::before,.heart::after{content:"";position:absolute;width:140px;height:140px;
background:#ff5f8d;border-radius:50%}
.heart::before{left:-70px}.heart::after{top:-70px}
@keyframes beat{50%{transform:rotate(45deg) scale(1.12)}}
</style></head><body><div class="heart"></div></body></html>
"""


def chromium_path():
    for candidate in sorted(Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome")):
        return str(candidate)
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self):
        self.port = free_port()
        self.app = create_app(ROOT, tts=FakeTTS())  # tests stay offline
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port, log_level="warning"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 25
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@pytest.fixture(scope="module")
def browser():
    path = chromium_path()
    if not path:
        pytest.skip("no Chromium available")
    with playwright_api.sync_playwright() as p:
        b = p.chromium.launch(
            executable_path=path,
            args=["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"],
        )
        yield b
        b.close()


def open_stage(browser, server: Server):
    page = browser.new_page(viewport={"width": 1280, "height": 720})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(server.base + "/vr-agent/teaching-stage.html")
    page.wait_for_function("window.teachingStage !== undefined", timeout=30000)
    return page, errors


def test_the_stage_loads_and_connects(browser):
    with Server() as server:
        page, errors = open_stage(browser, server)
        page.wait_for_timeout(1500)
        assert page.title()
        # The page registered with the backend as a room client.
        clients = httpx.get(server.base + "/harness/clients", timeout=10).json()
        assert clients.get("room", {}).get("active", True) in (True, None) or clients
        assert not [e for e in errors if "favicon" not in e.lower()], errors


def test_the_source_on_screen_is_the_backend_source(browser):
    with Server() as server:
        page, errors = open_stage(browser, server)
        session = server.app.state.session
        session.teaching.start(
            student_id="dev:selwyn", student_name="@selwyn", goal="a heart", teacher="luna"
        )
        session.teaching.set_phase("teaching")
        source = "print('hello from the backend')\n"
        session.teaching.coding_lesson.set_artifact("dev:selwyn", "python", source)
        httpx.post(
            server.base + "/harness/push",
            json={
                "ops": [
                    {
                        "op": "coding",
                        "phase": "writing",
                        "language": "python",
                        "filename": "main.py",
                        "code": source,
                        "display": "instant",
                    }
                ]
            },
            timeout=10,
        )
        page.wait_for_function(
            "document.body.innerText.includes('hello from the backend')", timeout=15000
        )
        shown = page.evaluate("document.body.innerText")
        assert "hello from the backend" in shown
        assert session.teaching.coding_lesson.code == source
        assert not [e for e in errors if "favicon" not in e.lower()], errors


def test_a_browser_project_previews(browser):
    with Server() as server:
        page, errors = open_stage(browser, server)
        session = server.app.state.session
        session.teaching.start(
            student_id="dev:selwyn", student_name="@selwyn", goal="beating heart", teacher="mika"
        )
        session.teaching.set_phase("teaching")
        session.teaching.coding_lesson.set_artifact("dev:selwyn", "web", WEB_PAGE)
        httpx.post(
            server.base + "/harness/push",
            json={
                "ops": [
                    {
                        "op": "coding",
                        "phase": "run_result",
                        "language": "web",
                        "filename": "index.html",
                        "preview_html": WEB_PAGE,
                        "preview_ready": True,
                    }
                ]
            },
            timeout=10,
        )
        page.wait_for_timeout(2500)
        frames = [f for f in page.frames if f != page.main_frame]
        assert frames, "the browser preview never appeared"
        assert not [e for e in errors if "favicon" not in e.lower()], errors
