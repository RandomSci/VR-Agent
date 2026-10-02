"""Publishing: off by default, gated, safe paths, no secrets, no duplicates.

GitHub and YouTube are simulated with httpx.MockTransport, so these tests
never touch the network or a real account.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from open_llm_vtuber.publishing import description as desc  # noqa: E402
from open_llm_vtuber.publishing.bundle import BundleError, build_bundle  # noqa: E402
from open_llm_vtuber.publishing.provenance import (  # noqa: E402
    PUBLISHED,
    CreationJob,
    is_safe_slug,
    safe_slug,
)
from open_llm_vtuber.publishing.service import PublicationService  # noqa: E402
from open_llm_vtuber.publishing.settings import PublishSettings  # noqa: E402
from open_llm_vtuber.publishing.targets import DryRunTarget, GitHubTarget, git_blob_sha  # noqa: E402
from open_llm_vtuber.publishing.youtube import YouTubeClient, YouTubeError  # noqa: E402
from open_llm_vtuber.room.capabilities import template_source  # noqa: E402

GAME = template_source("game-flyer")
TOKEN = "github_pat_SUPERSECRET123"
OK_CHECK = {"ok": True, "problems": [], "skipped": ""}


def settings(tmp_path, **overrides) -> PublishSettings:
    base = dict(
        enabled=True,
        dry_run=True,
        dry_run_dir=tmp_path / "dry",
        github_repo="selwyn/mika-generated-games",
        jobs_file=tmp_path / "jobs.json",
    )
    base.update(overrides)
    return PublishSettings(**base)


def passed_job(
    service: PublicationService, code: str = GAME, name: str = "@selwyn"
) -> CreationJob:
    job = service.record_creation(
        viewer={
            "platform": "youtube",
            "message_id": "m1",
            "display_name": name,
            "author_id": "UCabc",
        },
        request_text="make flappy bird where luna is the bird",
        title="Luna Flappy",
        kind="web_game_flyer",
        language="web",
    )
    service.record_result(job.job_id, code, OK_CHECK)
    return service.store.get(job.job_id)


# ---------------------------------------------------------------------------
# slugs and paths
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "MAKE MY GAME!!!! ../../../something",
        "/etc/passwd",
        "a; rm -rf / ; echo",
        "😀😀😀",
        "",
        "C:\\Windows\\system32",
        "..",
        "x" * 500,
        "Luna Flappy",
    ],
)
def test_viewer_text_never_becomes_a_path(text):
    slug = safe_slug(text)
    assert is_safe_slug(slug)
    assert "/" not in slug and "\\" not in slug and ".." not in slug and len(slug) <= 48


def test_slug_keeps_meaning():
    assert (
        safe_slug("MAKE MY GAME!!!! ../../../something", suffix="a82f")
        == "my-game-something-a82f"
    )
    assert safe_slug("Luna Flappy", suffix="0001") == "luna-flappy-0001"


def test_bundle_rewrites_stage_urls_and_copies_only_what_is_used():
    bundle = build_bundle(GAME, "luna-flappy-0001")
    page = bundle.files["games/luna-flappy-0001/index.html"].decode()
    assert "/stage-" not in page
    assert "../../libs/phaser/3.90.0/phaser.min.js" in page
    assert "Content-Security-Policy" in page
    assert "libs/phaser/3.90.0/phaser.min.js" in bundle.files
    assert "assets/sprites/bird.svg" in bundle.files
    assert "libs/three/0.186.1/three.module.js" not in bundle.files


@pytest.mark.parametrize(
    "bad",
    [
        '<script src="https://evil.example/x.js"></script>',
        '<img src="//evil.example/x.png">',
        '<img src="/stage-assets/../../conf.yaml">',
        '<img src="/stage-assets/sprites/nope.svg">',
    ],
)
def test_bundle_refuses_unsafe_programs(bad):
    with pytest.raises(BundleError):
        build_bundle(f"<html><head></head><body>{bad}</body></html>", "x-0001")
    with pytest.raises(BundleError):
        build_bundle(GAME, "../escape")


# ---------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------


def test_nothing_happens_while_disabled(tmp_path):
    service = PublicationService(settings(tmp_path, enabled=False))
    job = passed_job(service)
    outcome = service.publish_project(job.job_id, GAME)
    assert not outcome["ok"] and "disabled" in outcome["reason"]
    assert not (tmp_path / "dry").exists()


def test_defaults_are_all_off(monkeypatch):
    for name in (
        "PUBLISHING_ENABLED",
        "PUBLISHING_DRY_RUN",
        "PUBLISHING_AUTO",
        "YOUTUBE_PUBLISHING_ENABLED",
        "YOUTUBE_AUTO_CHAT_REPLY",
        "YOUTUBE_AUTO_DESCRIPTION",
        "YOUTUBE_AUTO_COMMENT_REPLY",
    ):
        monkeypatch.delenv(name, raising=False)
    s = PublishSettings.from_env(read_dotenv=False)
    assert not s.enabled and s.dry_run and not s.auto_publish
    assert not (
        s.youtube_enabled or s.youtube_chat_enabled or s.youtube_description_enabled
    )


def test_failed_builds_never_publish(tmp_path):
    service = PublicationService(settings(tmp_path))
    job = service.record_creation(
        viewer={},
        request_text="broken",
        title="broken",
        kind="web_game",
        language="web",
    )
    service.record_result(
        job.job_id, GAME, {"ok": False, "problems": ["JavaScript error: x"]}
    )
    outcome = service.publish_project(job.job_id, GAME)
    assert not outcome["ok"] and "passed" in outcome["reason"]


def test_code_changed_after_the_check_is_refused(tmp_path):
    service = PublicationService(settings(tmp_path))
    job = passed_job(service)
    outcome = service.publish_project(job.job_id, GAME + "<!-- edited -->")
    assert not outcome["ok"] and "changed" in outcome["reason"]


def test_python_is_not_published(tmp_path):
    service = PublicationService(settings(tmp_path))
    job = service.record_creation(
        viewer={},
        request_text="chart",
        title="chart",
        kind="python_chart",
        language="python",
    )
    service.record_result(job.job_id, "print(1)", OK_CHECK)
    assert not service.publish_project(job.job_id, "print(1)")["ok"]


# ---------------------------------------------------------------------------
# dry run: the whole flow, written to a local folder
# ---------------------------------------------------------------------------


def test_dry_run_publishes_a_playable_folder_and_gallery(tmp_path):
    service = PublicationService(settings(tmp_path))
    job = passed_job(service)
    outcome = service.publish_project(job.job_id, GAME, thumbnail_png=b"\x89PNG fake")
    assert outcome["ok"] and outcome["dry_run"]
    repo = tmp_path / "dry" / "mika-generated-games"
    slug = outcome["slug"]
    assert (repo / "games" / slug / "index.html").is_file()
    assert (repo / "libs/phaser/3.90.0/phaser.min.js").is_file()
    registry = json.loads((repo / "games.json").read_text())
    assert [g["slug"] for g in registry["games"]] == [slug]
    assert registry["games"][0]["requested_by"] == "@selwyn"
    gallery = (repo / "index.html").read_text()
    assert "Luna Flappy" in gallery and "Requested by @selwyn" in gallery
    assert service.store.get(job.job_id).status == PUBLISHED

    # Publishing the same job again never duplicates it.
    again = service.publish_project(job.job_id, GAME)
    assert again["ok"] and again["slug"] == slug
    assert len(json.loads((repo / "games.json").read_text())["games"]) == 1


def test_a_second_game_never_overwrites_the_first(tmp_path):
    service = PublicationService(settings(tmp_path))
    first = passed_job(service)
    second = passed_job(service, name="@ana")
    service.store.update(
        second.job_id, project_slug=first.project_slug
    )  # same slug on purpose
    a = service.publish_project(first.job_id, GAME)
    b = service.publish_project(second.job_id, GAME)
    assert a["ok"] and b["ok"] and a["slug"] != b["slug"]
    repo = tmp_path / "dry" / "mika-generated-games"
    assert len(json.loads((repo / "games.json").read_text())["games"]) == 2


def test_dry_run_announcement_and_description_are_only_logged(tmp_path):
    s = settings(
        tmp_path,
        youtube_enabled=True,
        youtube_chat_enabled=True,
        youtube_description_enabled=True,
        youtube_video_id="vid123",
    )
    service = PublicationService(
        s, youtube_factory=lambda: pytest.fail("no YouTube call in a dry run")
    )
    job = passed_job(service)
    service.publish_project(job.job_id, GAME)
    chat = service.announce_published_project(job.job_id)
    assert (
        chat["ok"]
        and chat["dry_run"]
        and chat["text"].startswith("@selwyn your game is up")
    )
    section = service.update_generated_games_section(job.job_id)
    assert section["ok"] and "Luna Flappy" in section["section"]


def test_dry_run_target_cannot_escape_its_folder(tmp_path):
    target = DryRunTarget(tmp_path / "repo")
    with pytest.raises(Exception):
        target.commit({"../outside.txt": b"x"}, "escape")
    assert not (tmp_path / "outside.txt").exists()


# ---------------------------------------------------------------------------
# description
# ---------------------------------------------------------------------------


def published_jobs(n: int) -> list[CreationJob]:
    return [
        CreationJob(
            job_id=f"j{i}",
            source="youtube_live_chat",
            title=f"Game <{i}>",
            viewer_display_name=f"@v{i}",
            project_slug=f"game-{i}-0000",
            public_url=f"https://s.github.io/g/games/game-{i}-0000/",
            status=PUBLISHED,
            published_at=1000 + i,
        )
        for i in range(n)
    ]


def test_description_keeps_human_text_and_is_idempotent():
    mine = "Welcome to the stream!\n\nFollow me on X.\n"
    jobs = published_jobs(7)
    once = desc.fit_description(mine, jobs, "https://s.github.io/g/", 5)
    twice = desc.fit_description(once, jobs, "https://s.github.io/g/", 5)
    assert once == twice
    assert once.startswith("Welcome to the stream!") and "Follow me on X." in once
    assert once.count(desc.START) == 1 and once.count("Game 6") == 1
    assert "Game 1" not in once  # only the latest five
    assert "<" not in once.split(desc.START)[1] and ">" not in once.split(desc.START)[1]


def test_description_stays_under_the_youtube_limit():
    long_text = "x" * 4700
    out = desc.fit_description(
        long_text, published_jobs(5), "https://s.github.io/g/", 5
    )
    assert len(out.encode("utf-8")) <= 5000
    assert out.startswith(long_text)


# ---------------------------------------------------------------------------
# GitHub, simulated
# ---------------------------------------------------------------------------


class FakeGitHub:
    def __init__(self, fail_on: str = ""):
        self.calls: list[tuple[str, str]] = []
        self.tree = {"README.md": git_blob_sha(b"hi")}
        self.fail_on = fail_on
        self.ref_update = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        if self.fail_on and self.fail_on in path:
            return httpx.Response(403, text=f"denied for token {TOKEN}")
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "head1"}})
        if path.endswith("/git/commits/head1"):
            return httpx.Response(200, json={"tree": {"sha": "tree1"}})
        if path.endswith("/git/trees/tree1"):
            return httpx.Response(
                200,
                json={
                    "tree": [
                        {"path": p, "sha": s, "type": "blob"}
                        for p, s in self.tree.items()
                    ]
                },
            )
        if path.endswith("/git/blobs") and method == "POST":
            data = base64.b64decode(json.loads(request.content)["content"])
            return httpx.Response(201, json={"sha": git_blob_sha(data)})
        if path.endswith("/git/trees") and method == "POST":
            return httpx.Response(201, json={"sha": "tree2"})
        if path.endswith("/git/commits") and method == "POST":
            body = json.loads(request.content)
            assert body["parents"] == ["head1"]
            return httpx.Response(201, json={"sha": "commit2"})
        if path.endswith("/git/refs/heads/main") and method == "PATCH":
            self.ref_update = json.loads(request.content)
            return httpx.Response(200, json={})
        return httpx.Response(404, text="unexpected " + path)


def test_github_publish_is_one_fast_forward_commit(tmp_path):
    fake = FakeGitHub()
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    s = settings(
        tmp_path,
        dry_run=False,
        github_token=TOKEN,
        pages_base_url="https://selwyn.github.io/mika-generated-games",
    )
    service = PublicationService(
        s, target_factory=lambda: GitHubTarget(s.github_repo, "main", TOKEN, client)
    )
    job = passed_job(service)
    outcome = service.publish_project(job.job_id, GAME)
    assert outcome["ok"], outcome
    assert outcome["url"].startswith(
        "https://selwyn.github.io/mika-generated-games/games/"
    )
    assert fake.ref_update == {"sha": "commit2", "force": False}
    assert [c for c in fake.calls if c[1].endswith("/git/commits")] == [
        ("POST", "/repos/selwyn/mika-generated-games/git/commits")
    ]


def test_github_errors_never_contain_the_token(tmp_path):
    fake = FakeGitHub(fail_on="/git/trees/tree1")
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    s = settings(
        tmp_path,
        dry_run=False,
        github_token=TOKEN,
        pages_base_url="https://x.github.io/g",
    )
    service = PublicationService(
        s, target_factory=lambda: GitHubTarget(s.github_repo, "main", TOKEN, client)
    )
    job = passed_job(service)
    outcome = service.publish_project(job.job_id, GAME)
    assert not outcome["ok"]
    assert TOKEN not in outcome["reason"] and "***" in outcome["reason"]
    assert TOKEN not in json.dumps(service.store.get(job.job_id).snapshot())
    assert (
        TOKEN not in repr(s)
        and "github_token" in s.describe()
        and s.describe()["github_token"] == "set"
    )


# ---------------------------------------------------------------------------
# YouTube, simulated
# ---------------------------------------------------------------------------


def youtube_client(handler) -> YouTubeClient:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(
                200, json={"access_token": "ya29.ACCESS", "expires_in": 3600}
            )
        return handler(request)

    return YouTubeClient(
        "cid",
        "SECRETCLIENT",
        "1//REFRESH",
        httpx.Client(transport=httpx.MockTransport(route)),
    )


def test_description_update_sends_back_the_whole_snippet():
    sent = {}

    def handler(request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "snippet": {
                                "title": "Mika codes live",
                                "categoryId": "28",
                                "tags": ["n8n"],
                                "description": "Hi!",
                                "channelId": "UC1",
                            }
                        }
                    ]
                },
            )
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={})

    client = youtube_client(handler)
    snippet = client.get_snippet("vid")
    client.set_description("vid", snippet, "Hi!\n\nnew")
    assert sent["snippet"]["title"] == "Mika codes live"
    assert sent["snippet"]["categoryId"] == "28" and sent["snippet"]["tags"] == ["n8n"]
    assert sent["snippet"]["description"] == "Hi!\n\nnew"


def test_chat_post_uses_the_active_chat_and_hides_secrets():
    posted = {}

    def handler(request):
        if request.url.path.endswith("/videos"):
            return httpx.Response(
                200,
                json={
                    "items": [{"liveStreamingDetails": {"activeLiveChatId": "chat1"}}]
                },
            )
        if request.url.path.endswith("/liveChat/messages"):
            posted.update(json.loads(request.content))
            return httpx.Response(200, json={})
        return httpx.Response(500, text="boom 1//REFRESH SECRETCLIENT")

    client = youtube_client(handler)
    chat = client.active_live_chat_id("vid")
    client.post_chat_message(chat, "@selwyn your game is live 🎮 https://x/")
    assert posted["snippet"]["liveChatId"] == "chat1"
    assert posted["snippet"]["textMessageDetails"]["messageText"].startswith("@selwyn")
    with pytest.raises(YouTubeError) as err:
        client.reply_to_comment("c1", "hi")
    assert "1//REFRESH" not in str(err.value) and "SECRETCLIENT" not in str(err.value)


def test_announcement_after_the_stream_ended_is_a_clean_no(tmp_path):
    s = settings(
        tmp_path,
        dry_run=False,
        github_token=TOKEN,
        pages_base_url="https://x.github.io/g",
        youtube_enabled=True,
        youtube_chat_enabled=True,
        youtube_video_id="vid",
        youtube_client_id="cid",
        youtube_client_secret="s",
        youtube_refresh_token="r",
    )
    ended = youtube_client(
        lambda r: httpx.Response(200, json={"items": [{"liveStreamingDetails": {}}]})
    )
    service = PublicationService(s, youtube_factory=lambda: ended)
    job = passed_job(service)
    service.store.update(
        job.job_id, status=PUBLISHED, public_url="https://x.github.io/g/games/a/"
    )
    outcome = service.announce_published_project(job.job_id)
    assert not outcome["ok"] and "not live" in outcome["reason"]


# ---------------------------------------------------------------------------
# on stream: request -> check -> record -> /publish
# ---------------------------------------------------------------------------


def test_a_viewer_request_becomes_a_credited_published_game(tmp_path):
    import asyncio
    import time as _time

    from open_llm_vtuber.room.browser_qa import BrowserReport
    from open_llm_vtuber.room.live_message import LiveMessage
    from tests.test_topic_switch import RecordingStage

    stage = RecordingStage()
    stage.start_session("mika", user="@ana")
    stage.runtimes.browser_check = lambda source: asyncio.sleep(
        0, BrowserReport(ok=True)
    )
    service = PublicationService(settings(tmp_path))
    stage.runtimes.publisher = service
    stage.decide(
        "flappy",
        json.dumps(
            {"action": "create", "kind": "web_game_flyer", "subject": "Luna Flappy"}
        ),
    )
    stage.code(GAME)

    message = LiveMessage(
        platform="youtube",
        message_id="yt-msg-1",
        username="@ana",
        text="make flappy bird where luna is the bird",
        timestamp=_time.time(),
        author_id="dev:ana",
    )

    async def go():
        stage.session.observe_viewer_message(message)
        plan = stage.session.director.plan(message)
        await stage.session.director.run(plan)
        return await stage.runtimes.publish_current(announce=False)

    outcome = asyncio.run(go())
    job = service.store.get(stage.lesson.creation_job_id)
    assert job.viewer_display_name == "@ana" and job.viewer_channel_id == "dev:ana"
    assert job.source == "youtube_live_chat" and job.source_message_id == "yt-msg-1"
    assert job.project_type == "web_game_flyer" and job.title == "Luna Flappy"
    assert outcome["ok"], outcome
    assert (
        tmp_path
        / "dry"
        / "mika-generated-games"
        / "games"
        / outcome["slug"]
        / "index.html"
    ).is_file()


# ---------------------------------------------------------------------------
# admin take-down
# ---------------------------------------------------------------------------


def test_admin_can_take_a_game_down(tmp_path):
    service = PublicationService(settings(tmp_path))
    keep = passed_job(service, name="@ana")
    service.publish_project(keep.job_id, GAME)
    gone = passed_job(service, code=GAME + "<!-- 2 -->", name="@bo")
    service.publish_project(gone.job_id, GAME + "<!-- 2 -->")
    slug = service.store.get(gone.job_id).project_slug
    folder = tmp_path / "dry" / "mika-generated-games"
    assert (folder / "games" / slug / "index.html").is_file()

    assert {g["slug"] for g in service.list_published()} >= {slug}
    result = service.unpublish(slug)
    assert result["ok"] and result["files"] >= 1
    assert not (folder / "games" / slug).exists() or not any(
        (folder / "games" / slug).rglob("*.html")
    )
    left = json.loads((folder / "games.json").read_text())
    assert slug not in {g["slug"] for g in left["games"]}
    assert service.store.get(keep.job_id).project_slug in {g["slug"] for g in left["games"]}
    assert slug not in (folder / "index.html").read_text()
    assert service.store.get(gone.job_id).status == "removed"
    assert not service.unpublish(slug)["ok"]  # already gone
    assert not service.unpublish("../etc")["ok"]


def test_github_delete_sends_null_shas():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path, request.content))
        path = request.url.path
        if path.endswith("/git/ref/heads/main"):
            return httpx.Response(200, json={"object": {"sha": "head"}})
        if path.endswith("/git/commits/head"):
            return httpx.Response(200, json={"tree": {"sha": "base"}})
        if "/git/trees/base" in path:
            return httpx.Response(
                200,
                json={"tree": [{"path": "games/x-1/index.html", "sha": "a", "type": "blob"}]},
            )
        if path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": "b"})
        if path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "t"})
        if path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "c"})
        return httpx.Response(200, json={})

    target = GitHubTarget(
        "o/r", "main", TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert target.list_paths("games/x-1/") == ["games/x-1/index.html"]
    target.commit({"games.json": b"{}"}, "Remove x-1", delete=("games/x-1/index.html",))
    tree = [json.loads(c) for m, p, c in calls if m == "POST" and p.endswith("/git/trees")][0]
    assert {"path": "games/x-1/index.html", "mode": "100644", "type": "blob", "sha": None} in tree["tree"]


def test_admin_routes_need_the_password(monkeypatch, tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from open_llm_vtuber.publishing import admin

    app = FastAPI()
    app.include_router(admin.init_admin_routes())
    client = TestClient(app)
    monkeypatch.delenv("PASS_W", raising=False)
    monkeypatch.delenv("pass_w", raising=False)
    assert client.post("/vr-agent/admin/games", json={"password": "x"}).status_code == 403
    monkeypatch.setenv("PASS_W", "hunter2")
    admin._failures.clear()
    assert client.post("/vr-agent/admin/games", json={"password": "nope"}).status_code == 401
    seen = []
    monkeypatch.setattr(
        admin, "_service", lambda: type("S", (), {"unpublish": lambda self, s: seen.append(s) or {"ok": True}})()
    )
    r = client.post("/vr-agent/admin/delete", json={"password": "hunter2", "slug": "x-1"})
    assert r.json()["ok"] and seen == ["x-1"]
    admin._failures.clear()
