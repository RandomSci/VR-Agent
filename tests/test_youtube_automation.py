"""YouTube with an empty YOUTUBE_VIDEO_ID and an empty YOUTUBE_REFRESH_TOKEN.

* The refresh token comes from data/secrets/youtube_token.json (owner-only),
  written once by the sign-in script and kept current by the server.
* The live stream is found on its own (liveBroadcasts.list, 1 unit), cached,
  looked up again when it ends, with the channel search only as a fallback.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from open_llm_vtuber.publishing import settings as settings_mod  # noqa: E402
from open_llm_vtuber.publishing.service import PublicationService  # noqa: E402
from open_llm_vtuber.publishing.settings import (  # noqa: E402
    PublishSettings,
    read_saved_refresh_token,
    save_refresh_token,
)
from open_llm_vtuber.publishing.youtube import YouTubeClient  # noqa: E402
from tests.test_publishing import GAME, passed_job  # noqa: E402

REFRESH = "1//refresh-SECRET-abc"
ENV_NAMES = (
    "YOUTUBE_REFRESH_TOKEN",
    "YOUTUBE_VIDEO_ID",
    "YOUTUBE_CHANNEL_ID",
    "YOUTUBE_CLIENT_ID",
    "YOUTUBE_CLIENT_SECRET",
)


def test_saved_token_is_private_and_read_back(tmp_path):
    path = tmp_path / "secrets" / "youtube_token.json"
    save_refresh_token(REFRESH, path)
    assert read_saved_refresh_token(path) == REFRESH
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert not [p for p in path.parent.iterdir() if p.name.startswith(".token-")]
    assert read_saved_refresh_token(tmp_path / "missing.json") == ""
    (tmp_path / "bad.json").write_text("not json")
    assert read_saved_refresh_token(tmp_path / "bad.json") == ""


def test_empty_env_token_falls_back_to_the_saved_file(tmp_path, monkeypatch):
    path = tmp_path / "youtube_token.json"
    save_refresh_token(REFRESH, path)
    monkeypatch.setattr(settings_mod, "TOKEN_FILE", path)
    for name in ENV_NAMES:
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("YOUTUBE_CLIENT_ID", "cid")
    monkeypatch.setenv("YOUTUBE_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("YOUTUBE_CHANNEL_ID", "UCchan")
    s = PublishSettings.from_env(read_dotenv=False)
    assert s.youtube_refresh_token == REFRESH and s.youtube_ready
    assert s.youtube_channel_id == "UCchan" and s.youtube_video_id == ""
    # Never shown.
    assert REFRESH not in repr(s) and s.describe()["youtube_refresh_token"] == "set"
    # A value in .env still wins.
    monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "from-env")
    assert (
        PublishSettings.from_env(read_dotenv=False).youtube_refresh_token == "from-env"
    )


class FakeGoogle:
    def __init__(self, live: list[str], search: str = "", rotate: str = ""):
        self.live = list(live)  # successive liveBroadcasts answers
        self.search = search
        self.rotate = rotate
        self.calls: list[str] = []
        self.posted: list[dict] = []
        self.chat_for = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path.rsplit("/", 1)[-1])
        if path.endswith("/token"):
            body = {"access_token": "at", "expires_in": 3600}
            if self.rotate:
                body["refresh_token"] = self.rotate
            return httpx.Response(200, json=body)
        if path.endswith("/liveBroadcasts"):
            assert request.url.params["broadcastStatus"] == "active"
            vid = self.live.pop(0) if len(self.live) > 1 else (self.live or [""])[0]
            return httpx.Response(200, json={"items": [{"id": vid}] if vid else []})
        if path.endswith("/search"):
            assert request.url.params["eventType"] == "live"
            items = [{"id": {"videoId": self.search}}] if self.search else []
            return httpx.Response(200, json={"items": items})
        if path.endswith("/videos"):
            vid = request.url.params["id"]
            chat = self.chat_for.get(vid, "")
            details = {"activeLiveChatId": chat} if chat else {}
            return httpx.Response(
                200, json={"items": [{"liveStreamingDetails": details}]}
            )
        if path.endswith("/messages"):
            self.posted.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "msg"})
        return httpx.Response(404)

    def client(self, saved=None) -> YouTubeClient:
        return YouTubeClient(
            "cid",
            "csecret",
            REFRESH,
            client=httpx.Client(transport=httpx.MockTransport(self.handler)),
            on_refresh_token=saved,
        )


def live_settings(tmp_path, **extra) -> PublishSettings:
    base = dict(
        enabled=True,
        dry_run=False,
        dry_run_dir=tmp_path / "dry",
        jobs_file=tmp_path / "jobs.json",
        youtube_enabled=True,
        youtube_chat_enabled=True,
        youtube_client_id="cid",
        youtube_client_secret="csecret",
        youtube_refresh_token=REFRESH,
        youtube_token_file=tmp_path / "secrets" / "youtube_token.json",
    )
    base.update(extra)
    return PublishSettings(**base)


def published(service: PublicationService):
    # Publishing itself goes to a dry-run folder; only YouTube is "real" here.
    service.settings.dry_run = True
    job = passed_job(service)
    assert service.publish_project(job.job_id, GAME)["ok"]
    service.settings.dry_run = False
    return job


def test_chat_finds_the_live_stream_without_a_video_id(tmp_path):
    google = FakeGoogle(live=["LIVE1"])
    google.chat_for["LIVE1"] = "chat-1"
    service = PublicationService(live_settings(tmp_path), youtube_factory=google.client)
    job = published(service)
    result = service.announce_published_project(job.job_id)
    assert result["ok"], result
    assert google.posted[0]["snippet"]["liveChatId"] == "chat-1"
    assert "search" not in google.calls  # the cheap lookup was enough
    # Cached: a second lookup within ten minutes costs nothing.
    before = google.calls.count("liveBroadcasts")
    assert service._video_id(google.client()) == "LIVE1"
    assert google.calls.count("liveBroadcasts") == before


def test_a_new_stream_is_picked_up_when_the_old_one_ends(tmp_path):
    google = FakeGoogle(live=["NEW"])
    google.chat_for["NEW"] = "chat-new"  # OLD has ended: no chat
    service = PublicationService(live_settings(tmp_path), youtube_factory=google.client)
    service._found_video_id, service._found_at = "OLD", 10**12
    job = published(service)
    assert service.announce_published_project(job.job_id)["ok"]
    assert google.posted[0]["snippet"]["liveChatId"] == "chat-new"


def test_channel_search_is_only_the_fallback(tmp_path):
    google = FakeGoogle(live=[""], search="FOUND")
    google.chat_for["FOUND"] = "chat-f"
    service = PublicationService(
        live_settings(tmp_path, youtube_channel_id="UCchan"),
        youtube_factory=google.client,
    )
    job = published(service)
    assert service.announce_published_project(job.job_id)["ok"]
    assert google.calls.index("liveBroadcasts") < google.calls.index("search")


def test_not_live_is_a_calm_no(tmp_path):
    google = FakeGoogle(live=[""])
    service = PublicationService(live_settings(tmp_path), youtube_factory=google.client)
    job = published(service)
    result = service.announce_published_project(job.job_id)
    assert not result["ok"] and "not live" in result["reason"]
    assert "search" not in google.calls  # no channel id, no 100-unit search


def test_a_set_video_id_still_wins(tmp_path):
    google = FakeGoogle(live=["OTHER"])
    google.chat_for["PINNED"] = "chat-p"
    service = PublicationService(
        live_settings(tmp_path, youtube_video_id="PINNED"),
        youtube_factory=google.client,
    )
    job = published(service)
    assert service.announce_published_project(job.job_id)["ok"]
    assert "liveBroadcasts" not in google.calls


def test_dry_run_needs_no_video_id_or_credentials(tmp_path):
    s = live_settings(
        tmp_path,
        dry_run=True,
        youtube_client_id="",
        youtube_client_secret="",
        youtube_refresh_token="",
    )
    service = PublicationService(
        s, youtube_factory=lambda: pytest.fail("no YouTube call in a dry run")
    )
    job = passed_job(service)
    service.publish_project(job.job_id, GAME)
    result = service.announce_published_project(job.job_id)
    assert result["ok"] and result["dry_run"]


def test_a_rotated_refresh_token_is_saved(tmp_path):
    google = FakeGoogle(live=["LIVE1"], rotate="1//rotated-NEW")
    google.chat_for["LIVE1"] = "chat-1"
    s = live_settings(tmp_path)
    service = PublicationService(s)  # the real factory, which saves rotations
    service._youtube_factory = lambda: YouTubeClient(
        s.youtube_client_id,
        s.youtube_client_secret,
        s.youtube_refresh_token,
        client=httpx.Client(transport=httpx.MockTransport(google.handler)),
        on_refresh_token=service._default_youtube()._on_refresh_token,
    )
    job = published(service)
    assert service.announce_published_project(job.job_id)["ok"]
    assert read_saved_refresh_token(s.youtube_token_file) == "1//rotated-NEW"
    assert s.youtube_refresh_token == "1//rotated-NEW"


def test_the_token_file_is_gitignored():
    assert "data/secrets/" in (ROOT / ".gitignore").read_text()