"""DEV publishing: every passing web build goes to the games site, a change
updates the same page, and the terminal shows what YouTube would get.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from open_llm_vtuber.publishing.provenance import PUBLISHED  # noqa: E402
from open_llm_vtuber.publishing.service import PublicationService  # noqa: E402
from open_llm_vtuber.room.browser_qa import BrowserReport  # noqa: E402
from tests.test_publishing import GAME, passed_job, settings  # noqa: E402


def test_youtube_preview_is_the_chat_line_and_the_description(tmp_path):
    service = PublicationService(settings(tmp_path))
    job = passed_job(service)
    assert service.publish_project(job.job_id, GAME)["ok"]
    preview = service.youtube_preview(job.job_id)
    assert preview["chat"].startswith("@selwyn your game is up")
    assert "Luna Flappy" in preview["description"]


def test_a_broken_change_keeps_the_published_game_listed(tmp_path):
    service = PublicationService(settings(tmp_path))
    job = passed_job(service)
    service.publish_project(job.job_id, GAME)
    service.record_result(job.job_id, GAME + "<!-- broken -->", {"ok": False})
    assert service.store.get(job.job_id).status == PUBLISHED
    assert [j.job_id for j in service.store.published()] == [job.job_id]


def test_auto_publish_then_a_change_updates_the_same_page(tmp_path):
    from tests.test_topic_switch import RecordingStage

    stage = RecordingStage()
    stage.start_session("mika")

    async def ok(source, **_):
        return BrowserReport(ok=True)

    stage.runtimes.browser_check = ok
    service = PublicationService(settings(tmp_path, auto_publish=True))
    stage.runtimes.publisher = service

    import time

    from open_llm_vtuber.room.live_message import LiveMessage

    async def build(marker, decision, code, text):
        """One viewer request, then wait for the automatic publish it starts."""
        stage.decide(marker, json.dumps(decision))
        stage.code(code)
        message = LiveMessage(
            platform="dev",
            message_id=f"c-{time.time_ns()}",
            username="@selwyn",
            text=text,
            timestamp=time.time(),
            author_id="dev:selwyn",
        )
        stage.runtimes.publish_task = None
        plan = stage.session.director.plan(message)
        await stage.session.director.run(plan)
        if stage.runtimes.publish_task is not None:
            await stage.runtimes.publish_task
        events = list(stage.runtimes.publish_events)
        stage.runtimes.publish_events.clear()
        return events

    async def scenario():
        first = await build(
            "flappy",
            {"action": "create", "kind": "web_game_flyer", "subject": "Luna Flappy"},
            GAME,
            "make flappy bird",
        )
        second = await build(
            "faster",
            {"action": "modify", "subject": "faster"},
            GAME.replace("speed: 300", "speed: 420"),
            "make it faster",
        )
        return first, second

    first, second = asyncio.run(scenario())
    assert first and first[0]["ok"] and not first[0]["updated"], first
    assert first[0]["preview"]["chat"]
    assert second and second[0]["ok"] and second[0]["updated"], second
    assert second[0]["url"] == first[0]["url"]


def test_gallery_titles_drop_the_building_words():
    from open_llm_vtuber.publishing.service import clean_title

    assert clean_title("building a new space shooter game") == "Space shooter game"
    assert clean_title("Luna Flappy") == "Luna Flappy"
    assert clean_title("new") == "New"


def test_real_links_carry_a_version_so_updates_show_at_once(tmp_path):
    from open_llm_vtuber.publishing.targets import DryRunTarget

    s = settings(
        tmp_path,
        dry_run=False,
        github_token="t",
        pages_base_url="https://x.github.io/g",
    )
    target = DryRunTarget(tmp_path / "repo")
    service = PublicationService(s, target_factory=lambda: target)
    job = passed_job(service)
    first = service.publish_project(job.job_id, GAME)
    assert first["ok"] and "?v=" in first["url"] and first["page"].endswith("/")
    assert "?v=" not in service.store.get(job.job_id).public_url
