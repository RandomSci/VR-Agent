"""Going back to an earlier game, and who may change it.

* "go back to the flappy game and make it faster" reopens that exact game on
  the Stage, edits it and updates its original page (same link).
* The host, or the viewer who asked for the game, changes the original.
  Anyone else gets a remix: a new game that credits the original.
* A vague request is a change to what is on screen (prompt rule).
* Older games without a local copy are read back from the published repo.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from open_llm_vtuber.publishing.service import PublicationService  # noqa: E402
from open_llm_vtuber.room.browser_qa import BrowserReport  # noqa: E402
from open_llm_vtuber.room.coding_actions import DECIDE_SYSTEM, parse_decision  # noqa: E402
from open_llm_vtuber.room.live_message import LiveMessage  # noqa: E402
from tests.test_publishing import GAME, settings  # noqa: E402
from tests.test_topic_switch import RecordingStage  # noqa: E402

SHOOTER = GAME.replace("Night Flyer", "Star Shooter")


def stage_with_publisher(tmp_path):
    stage = RecordingStage()
    stage.start_session("luna")

    async def ok(source, **_):
        return BrowserReport(ok=True)

    stage.runtimes.browser_check = ok
    service = PublicationService(settings(tmp_path, auto_publish=True))
    stage.runtimes.publisher = service
    return stage, service


async def request(stage, marker, decision, code, text, platform="dev", author="dev:selwyn"):
    stage.decide(marker, json.dumps(decision))
    if code is not None:
        stage.code(code)
    message = LiveMessage(
        platform=platform,
        message_id=f"c-{time.time_ns()}",
        username="@selwyn",
        text=text,
        timestamp=time.time(),
        author_id=author,
    )
    stage.runtimes.publish_task = None
    plan = stage.session.director.plan(message)
    await stage.session.director.run(plan)
    if stage.runtimes.publish_task is not None:
        await stage.runtimes.publish_task
    events = list(stage.runtimes.publish_events)
    stage.runtimes.publish_events.clear()
    return events


def test_the_decision_can_name_an_earlier_game():
    assert "published_games" in DECIDE_SYSTEM and "reopen" in DECIDE_SYSTEM
    d = parse_decision('{"action":"modify","reopen":"ab12cd","fresh":true}')
    assert d.reopen == "ab12cd" and d.fresh is False
    assert parse_decision('{"action":"chat","reopen":"ab12cd"}').reopen == ""
    assert "could mean either" in DECIDE_SYSTEM  # vague means change


def test_going_back_updates_the_original_game(tmp_path):
    stage, service = stage_with_publisher(tmp_path)

    async def scenario():
        first = await request(
            stage,
            "flappy",
            {"action": "create", "kind": "web_game_flyer", "subject": "Luna Flappy"},
            GAME,
            "make flappy bird",
        )
        await request(
            stage,
            "shooter",
            {"action": "create", "kind": "web_game_shooter", "fresh": True,
             "subject": "Star shooter"},
            SHOOTER,
            "now a new space shooter",
        )
        flappy_id = first[0]["slug"]
        job = next(j for j in service.store.published() if j.project_slug == flappy_id)
        # The decision sees both games and names the flappy one.
        situation = stage.runtimes._coding_context({})
        assert {g["id"] for g in situation["published_games"]} >= {job.job_id}
        faster = GAME.replace("speed: 300", "speed: 450")
        events = await request(
            stage,
            "return to",
            {"action": "modify", "reopen": job.job_id, "subject": "faster"},
            faster,
            "return to the bird one and speed it up",
        )
        return first, events, job

    first, events, job = asyncio.run(scenario())
    # The flappy game was put back on screen and handed to the writer.
    request_sent = stage.generate_requests[-1]
    assert "Night Flyer" in request_sent["current_program"]
    assert events and events[0]["updated"] and events[0]["slug"] == job.project_slug
    assert "speed: 450" in service.saved_code(job.job_id)
    assert stage.lesson.creation_job_id == job.job_id


def test_someone_else_gets_a_remix(tmp_path):
    stage, service = stage_with_publisher(tmp_path)

    async def scenario():
        await request(
            stage,
            "flappy",
            {"action": "create", "kind": "web_game_flyer", "subject": "Luna Flappy"},
            GAME,
            "make flappy bird",
        )
        original = service.store.published()[0]
        # A different person now owns the session and asks for a change.
        stage.session.teaching.finish()
        stage.session.teaching.start(
            student_id="yt:ana", student_name="@ana", goal="code", teacher="luna"
        )
        stage.session.teaching.set_phase("teaching")
        lesson_context = {
            "reopen": original.job_id,
            "student_id": "yt:ana",
            "viewer": {"platform": "youtube", "author_id": "yt:ana"},
        }
        reopened = await stage.runtimes._reopen(stage.lesson, lesson_context)
        return original, reopened

    original, reopened = asyncio.run(scenario())
    assert reopened["remix"] and reopened["remix_of"] == original.title
    assert stage.lesson.creation_job_id == ""  # a new game, never the original
    assert "Night Flyer" in stage.source


def test_older_games_are_read_back_from_the_repo(tmp_path):
    stage, service = stage_with_publisher(tmp_path)

    async def scenario():
        await request(
            stage,
            "flappy",
            {"action": "create", "kind": "web_game_flyer", "subject": "Luna Flappy"},
            GAME,
            "make flappy bird",
        )

    asyncio.run(scenario())
    job = service.store.published()[0]
    (service.code_dir / f"{job.job_id}.html").unlink()  # like a game from before
    code = service.saved_code(job.job_id)
    assert "/stage-libs/phaser/" in code and "../../" not in code
    assert "Content-Security-Policy" not in code.split("<head>", 1)[-1][:400]


def test_luna_is_credited_when_she_builds_it(tmp_path):
    stage, service = stage_with_publisher(tmp_path)

    async def scenario():
        await request(
            stage,
            "flappy",
            {"action": "create", "kind": "web_game_flyer", "subject": "Luna Flappy"},
            GAME,
            "make flappy bird",
        )

    asyncio.run(scenario())
    job = service.store.published()[0]
    assert job.built_by == "luna"
    preview = service.youtube_preview(job.job_id)
    assert "MIKA AND LUNA" in preview["description"]
    assert "Built by Luna" in preview["description"]
