"""The last pre-live pieces: gallery search and sharing, roast-and-build
attitude, and the optional code-only model with a safe fallback.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from open_llm_vtuber.publishing.gallery import render_gallery  # noqa: E402
from open_llm_vtuber.room.code_model import CodeModel, code_model_from_env  # noqa: E402
from open_llm_vtuber.room.director import build_attitude  # noqa: E402

REGISTRY = {
    "games": [
        {
            "slug": "space-shooter-a34e",
            "title": "building a new space shooter game",
            "requested_by": "@ana",
            "built_by": "luna",
            "has_thumbnail": True,
        },
        {"slug": "x-1234", "title": "<b>evil</b>", "requested_by": "@bo"},
    ]
}


def test_gallery_has_search_share_and_the_live_link():
    page = render_gallery(REGISTRY, "https://www.youtube.com/@SelwynBuilds-j1s")
    assert 'id="search"' in page and "Share with friends" in page
    assert "https://www.youtube.com/@SelwynBuilds-j1s" in page
    assert "Mika and Luna" in page and "Built by Luna" in page
    assert "Space shooter game" in page  # cleaned title
    assert "<b>evil" not in page  # escaped
    assert 'data-search="space shooter game @ana luna"' in page
    # A bad live link is simply left out.
    assert "javascript:" not in render_gallery(REGISTRY, "javascript:alert(1)")


def test_attitude_fits_what_happened():
    ok = build_attitude("mika", {"check": {"ok": True}})
    assert "roast" in ok and "never the person" in ok and "dramatic" in ok
    broken = build_attitude("luna", {"check": {"ok": False, "problems": ["x"]}})
    assert (
        "upset" in broken and "deadpan" in broken and "never blame the viewer" in broken
    )
    fixed = build_attitude("mika", {"check": {"ok": True}, "repaired": True})
    assert "relief or triumph" in fixed
    crashed = build_attitude("mika", {"run_result": {"exit_code": 1}})
    assert "upset" in crashed


def test_code_model_is_off_unless_set(monkeypatch):
    monkeypatch.delenv("VR_CODE_MODEL", raising=False)
    assert code_model_from_env() is None
    monkeypatch.setenv("VR_CODE_MODEL", "gpt-5-mini")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    model = code_model_from_env()
    assert model is not None and model.reasoning_model
    request = model.request([{"role": "user", "content": "hi"}])
    assert request["reasoning_effort"] == "low" and "temperature" not in request
    plain = CodeModel("gpt-4.1-mini", "k").request([])
    assert "temperature" in plain and "reasoning_effort" not in plain


def test_a_failing_code_model_falls_back_to_the_chat_model():
    from tests.test_repair import FIXED_WEB, web_stage

    class Broken:
        model = "gpt-5-mini"

        async def chat_completion(self, messages, system=None, tools=None):
            raise RuntimeError("quota")
            yield ""  # pragma: no cover

    class Answers:
        model = "gpt-5-mini"

        def __init__(self):
            self.calls = 0

        async def chat_completion(self, messages, system=None, tools=None):
            self.calls += 1
            yield FIXED_WEB + "<!-- teal -->"

    stage = web_stage(FIXED_WEB)
    stage.runtimes.code_model = Broken()
    code = asyncio.run(stage.runtimes.generate_code("mika", "web", "a heart"))
    assert code.strip() == FIXED_WEB.strip()  # the chat model wrote it

    better = Answers()
    stage.runtimes.code_model = better
    code = asyncio.run(stage.runtimes.generate_code("mika", "web", "a heart"))
    assert better.calls == 1 and "teal" in code


def test_client_is_built_with_the_right_arguments():
    seen = {}

    class Completions:
        async def create(self, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=SimpleNamespace(content="<html></html>"))
                ]
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    model = CodeModel("gpt-5-mini", "k", reasoning="minimal", client=client)

    async def run():
        return [
            c async for c in model.chat_completion([{"role": "user", "content": "x"}])
        ]

    assert asyncio.run(run()) == ["<html></html>"]
    assert seen["model"] == "gpt-5-mini" and seen["reasoning_effort"] == "minimal"


def test_live_chat_build_requests_start_and_hand_over_the_session():
    """On stream nobody says "start a lesson": a build request just works,
    and the next viewer who asks for a build becomes the owner."""
    import json as _json
    import time as _time

    from open_llm_vtuber.room.live_message import LiveMessage
    from tests.test_code_in_public import HEART_WEB
    from tests.test_topic_switch import RecordingStage

    stage = RecordingStage()  # no session started on purpose
    stage.decide(
        "heart",
        _json.dumps({"action": "create", "kind": "web_canvas", "subject": "heart"}),
    )
    stage.decide(
        "stars",
        _json.dumps(
            {"action": "create", "kind": "web_canvas", "fresh": True, "subject": "stars"}
        ),
    )

    def say(text, author):
        message = LiveMessage(
            platform="youtube",
            message_id=f"m-{_time.time_ns()}",
            username=author,
            text=text,
            timestamp=_time.time(),
            author_id=f"yt:{author}",
        )

        async def go():
            plan = stage.session.director.plan(message)
            await stage.session.director.run(plan)

        asyncio.run(go())

    stage.code(HEART_WEB.replace("COLOUR", "red"))
    say("make a heart page", "@ana")
    assert stage.session.teaching.session.active
    assert stage.session.teaching.owns("yt:@ana")
    assert "red" in stage.source

    stage.code(HEART_WEB.replace("COLOUR", "gold"))
    say("now make stars", "@bo")
    assert stage.session.teaching.owns("yt:@bo")
    assert "gold" in stage.source


def test_our_own_channel_and_banned_names_are_never_read(monkeypatch):
    from datetime import datetime, timezone

    from open_llm_vtuber.live.youtube_live import YouTubeChatMessage, blocked_author_reason

    def msg(author, text, author_type=""):
        return YouTubeChatMessage(
            message_id="x",
            author_channel_id="name:" + author.lower(),
            author_display_name=author,
            text=text,
            timestamp=datetime.now(timezone.utc),
            author_type=author_type,
        )

    monkeypatch.delenv("VR_IGNORE_OWNER", raising=False)
    monkeypatch.setenv("VR_BANNED_AUTHORS", "@SelwynBuilds-j1s, spam bot")
    assert blocked_author_reason(msg("@SelwynBuilds-j1s", "make a game")) == "banned"
    assert blocked_author_reason(msg("@spambot", "hi")) == "banned"
    # the channel owner can talk to the girls too (only what the system posted itself is skipped)
    assert blocked_author_reason(msg("@owner", "hi", "owner")) == ""
    echo = "@ana your game is up 🎮 https://x.github.io/g/ (give it about 5 minutes)"
    assert blocked_author_reason(msg("@someone", echo)) == "our own announcement"
    assert blocked_author_reason(msg("@MathUnlockedYT", "build a snake game")) == ""
    monkeypatch.setenv("VR_IGNORE_OWNER", "1")
    assert blocked_author_reason(msg("@owner", "hi", "owner")) == "channel owner"


def test_gallery_promo_waits_for_a_crowd_and_never_spams(monkeypatch):
    from open_llm_vtuber.publishing.promo import GalleryPromoter

    for name in ("VR_GALLERY_PROMO", "VR_PROMO_MIN_VIEWERS", "VR_PROMO_EVERY_MINUTES"):
        monkeypatch.delenv(name, raising=False)
    now = [1000.0]
    posts = []

    class Service:
        def post_gallery_promo(self, template):
            posts.append(template)
            return {"ok": True}

    p = GalleryPromoter(clock=lambda: now[0], service_factory=Service)
    now[0] += 11 * 60  # past the quiet start
    assert not p.note("@ana") and not p.note("@ana") and not p.note("@bo")
    assert p.note("@cy")  # three different people are chatting
    assert p.post()["ok"] and len(posts) == 1
    now[0] += 5 * 60
    assert not p.note("@dee")  # too soon after the last post
    now[0] += 16 * 60
    p.note("@ana")  # the earlier chatters have expired by now
    for who in ("@e", "@f", "@g"):
        p.note(who)
    assert p.note("@h")
    monkeypatch.setenv("VR_GALLERY_PROMO", "0")
    assert not p.note("@i")
