"""Kinds, libraries, templates and assets: what Mika builds with.

* The decision's kind reaches the code writer with the right guide, library,
  template and approved asset list, and nothing is invented.
* Templates are only sent for new programs; a change edits what is on screen.
* Every URL a template or the catalog mentions exists on disk.
* Every template passes the real browser check (sandbox, policy, autopilot).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from open_llm_vtuber.room import capabilities as cap  # noqa: E402
from open_llm_vtuber.room.coding_actions import DECIDE_SYSTEM, parse_decision  # noqa: E402
from tests.test_code_in_public import HEART_WEB  # noqa: E402
from tests.test_topic_switch import RecordingStage  # noqa: E402

FRONTEND = ROOT / "frontend"
STAGE_URL = re.compile(r"/stage-(?:libs|assets|templates)/[A-Za-z0-9_./-]+")


def stage_urls(text: str) -> set[str]:
    return set(STAGE_URL.findall(text))


def test_every_kind_is_offered_to_the_decision_and_parsed_back():
    for name in cap.KINDS:
        assert name in DECIDE_SYSTEM
        decision = parse_decision(json.dumps({"action": "create", "kind": name}))
        assert decision.kind == name
        assert decision.language == cap.KINDS[name].language
    assert parse_decision('{"action":"chat","kind":"web_3d"}').kind == ""
    assert parse_decision('{"action":"create","kind":"made_up"}').kind == ""


def test_libraries_are_pinned_and_present():
    manifest = cap.library_manifest()["libraries"]
    assert set(manifest) == {"p5", "phaser", "three", "chartjs", "matter"}
    for name, lib in manifest.items():
        path = FRONTEND / lib["url"].lstrip("/")
        assert path.is_file(), lib["url"]
        assert lib["version"] in lib["url"], "every URL is pinned to a version"
        licence = list(path.parent.glob("LICENSE*")) + list(
            path.parent.glob("license*")
        )
        assert licence, f"{name} ships without its license"


def test_every_url_in_templates_and_catalog_exists():
    for asset in cap.asset_catalog():
        assert (FRONTEND / asset["url"].lstrip("/")).is_file(), asset["url"]
    for template in cap.list_templates():
        source = cap.template_source(template["name"])
        urls = stage_urls(source)
        if template["libraries"]:
            assert urls, f"{template['name']} loads none of its libraries"
        for url in urls:
            assert (FRONTEND / url.lstrip("/")).is_file(), f"{template['name']}: {url}"
        # Nothing from the internet.
        assert not re.search(r"""(?:src|href)=["']https?://""", source), template[
            "name"
        ]


def test_discovery_functions_return_real_things():
    sprites = cap.list_available_assets("sprites")
    assert sprites and all(a["kind"] == "sprites" for a in sprites)
    found = cap.search_assets("luna bird")
    assert found and found[0]["key"] in ("luna-head", "bird")
    names = {t["name"] for t in cap.list_templates()}
    assert {
        "game-flyer",
        "game-shooter",
        "creative-p5",
        "scene-3d",
        "physics-matter",
        "dashboard-chart",
        "landing-page",
        "site-scroll",
        "game-arena",
    } <= names
    info = cap.get_template_info("game-flyer")
    assert info and "SETTINGS" in info["source"]
    assert cap.get_template_info("../../conf") is None
    assert cap.template_source("../../conf") == ""


def test_a_new_game_gets_its_template_assets_and_library():
    stage = RecordingStage()
    stage.start_session("mika")
    stage.decide(
        "flappy",
        json.dumps(
            {
                "action": "create",
                "kind": "web_game_flyer",
                "subject": "Luna flappy bird",
                "brief": "Flappy Bird where Luna is the bird.",
            }
        ),
    )
    stage.code(HEART_WEB.replace("COLOUR", "gold"))
    stage.say("make a flappy bird game where luna is the bird")

    request = stage.generate_requests[-1]
    assert request["kind"] == "web_game_flyer"
    assert "SETTINGS" in request["starting_template"]
    assert "/stage-libs/phaser/3.90.0/phaser.min.js" in request["libraries"]
    assert "/stage-assets/characters/luna-head.png" in request["approved_assets"]
    assert stage.lesson.kind == "web_game_flyer"

    # A change keeps the kind but does not resend the template.
    stage.decide(
        "faster",
        json.dumps(
            {
                "action": "modify",
                "subject": "faster",
                "brief": "Make the pipes come faster.",
            }
        ),
    )
    stage.say("make it faster")
    request = stage.generate_requests[-1]
    assert request["kind"] == "web_game_flyer"
    assert "starting_template" not in request
    assert "current_program" in request


def test_a_python_lesson_never_asks_for_a_chart():
    stage = RecordingStage()
    stage.start_session("mika")
    stage.decide(
        "lists",
        json.dumps(
            {"action": "create", "kind": "python_lesson", "subject": "Python lists"}
        ),
    )
    stage.code('snacks = ["chips"]\nprint(snacks)\n')
    stage.say("teach me python lists")
    request = stage.generate_requests[-1]
    assert request["kind"] == "python_lesson"
    assert "starting_template" not in request and "approved_assets" not in request


# ---------------------------------------------------------------------------
# every template in a real browser
# ---------------------------------------------------------------------------


def _chromium() -> str | None:
    if os.environ.get("VR_QA_CHROMIUM"):
        return os.environ["VR_QA_CHROMIUM"]
    for candidate in sorted(
        Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome")
    ):
        return str(candidate)
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return (
                p.chromium.executable_path
                if Path(p.chromium.executable_path).exists()
                else None
            )
    except Exception:
        return None


@pytest.mark.skipif(
    _chromium() is None, reason="Chromium for Playwright is not installed"
)
def test_every_template_passes_the_browser_check(monkeypatch):
    import uvicorn

    from open_llm_vtuber.room.browser_qa import BrowserQA
    from tests.harness.fake_tts import FakeTTS
    from tests.harness.full_dev_server import create_app

    monkeypatch.setenv("VR_QA_CHROMIUM", _chromium())
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(ROOT, tts=FakeTTS()),
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)

    async def run_all():
        qa = BrowserQA(f"http://127.0.0.1:{port}", timeout_seconds=40)
        try:
            return {
                t["name"]: await qa.check(
                    cap.template_source(t["name"]),
                    allow_scroll=t["kind"] == "web_site",
                )
                for t in cap.list_templates()
            }
        finally:
            await qa.close()

    try:
        reports = asyncio.run(run_all())
    finally:
        server.should_exit = True
    failures = {name: r.problems() for name, r in reports.items() if not r.ok}
    assert not failures, failures


def test_a_browser_that_cannot_start_skips_the_check_instead_of_blaming_the_program(
    monkeypatch,
):
    from open_llm_vtuber.room.browser_qa import BrowserQA

    monkeypatch.setenv("VR_QA_CHROMIUM", "/nonexistent/chrome")
    qa = BrowserQA("http://127.0.0.1:9")
    report = asyncio.run(qa.check("<html></html>"))
    assert report.ok and report.skipped  # no repair is triggered by this


@pytest.mark.skipif(
    _chromium() is None, reason="Chromium for Playwright is not installed"
)
def test_a_frozen_page_is_a_real_problem(monkeypatch):
    import uvicorn

    from open_llm_vtuber.room.browser_qa import BrowserQA
    from tests.harness.fake_tts import FakeTTS
    from tests.harness.full_dev_server import create_app

    monkeypatch.setenv("VR_QA_CHROMIUM", _chromium())
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(ROOT, tts=FakeTTS()),
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)

    async def run():
        qa = BrowserQA(f"http://127.0.0.1:{port}", timeout_seconds=8)
        try:
            frozen = await qa.check(
                "<html><body><script>while (true) {}</script></body></html>"
            )
            blank = await qa.check("<html><body style='background:#000'></body></html>")
            broken = await qa.check(
                "<html><body><canvas></canvas><script>missingFunction()</script></body></html>"
            )
            moving = (
                "<style>h1{animation:s 1s infinite alternate}@keyframes s{to{transform:"
                "translateX(80px)}}</style><h1>Hi</h1>"
            )
            dead = await qa.check(
                f"<html><body>{moving}<a class=btn>Book a call</a>"
                "<a href='#nowhere'>Pricing</a><button>Buy</button></body></html>"
            )
            alive = await qa.check(
                f"<html><body>{moving}<a href='#x'>Go</a><div id=x>x</div>"
                "<button onclick='1'>Buy</button><button class=d>Del</button>"
                "<script>document.addEventListener('click',()=>{})</script></body></html>"
            )
            return frozen, blank, broken, dead, alive
        finally:
            await qa.close()

    try:
        frozen, blank, broken, dead, alive = asyncio.run(run())
    finally:
        server.should_exit = True
    assert not dead.ok and len(dead.dead_controls) == 3, dead.problems()
    assert "Book a call" in dead.problems()[-1]
    assert not alive.dead_controls, alive.problems()
    assert not frozen.ok and not frozen.skipped
    assert not blank.ok and blank.blank
    assert not broken.ok and any("missingFunction" in e for e in broken.errors)
