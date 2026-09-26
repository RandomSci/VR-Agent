"""Zero-activity mode: no viewer messages means 0 LLM requests and 0 TTS requests.

These tests drive the real RoomSession (directors, Game Engine, ShowRunner,
Speaking Coordinator with a counted TTS engine) and the real YouTube reply
loop on simulated time, then check the usage meter.
"""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path
from types import SimpleNamespace

from open_llm_vtuber.live.youtube_live import YouTubeLiveChatService
from open_llm_vtuber.message_handler import message_handler
from open_llm_vtuber.room.live_message import LiveMessage
from open_llm_vtuber.room.profiles import load_room
from open_llm_vtuber.room.session import RoomSession
from open_llm_vtuber.vr_agent.state import runtime
from open_llm_vtuber.vr_agent.usage import usage
from tests.harness.fake_tts import FakeTTS

ROOT = Path(__file__).resolve().parents[1]
THIRTY_MINUTES = 30 * 60


class Clock:
    def __init__(self, t: float = 10_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class Sim:
    """A connected room page on simulated time. Replies to playback like room.js."""

    def __init__(self):
        self.clock = Clock()
        self.session = RoomSession(
            load_room(ROOT / "room", ROOT), rng=random.Random(1), clock=self.clock
        )
        self.tts = FakeTTS()
        self.session.voices.factory = lambda engine_type, **kwargs: self.tts
        self.session.configure_voices(None, self.tts)
        self.session.show.sleep = self._sleep
        self.sent: list[dict] = []
        self.uid = "sim-room-page"

    async def _sleep(self, seconds: float) -> None:
        self.clock.t += seconds
        await asyncio.sleep(0)

    async def _send(self, text: str) -> None:
        payload = json.loads(text)
        self.sent.append(payload)
        if payload.get("type") == "backend-synth-complete":
            asyncio.get_running_loop().call_soon(
                message_handler.handle_message,
                self.uid,
                {"type": "frontend-playback-complete"},
            )

    async def connect(self) -> None:
        await self.session.register(self.uid, self._send)
        if self.session._loop_task:  # we drive the clock ourselves
            self.session._loop_task.cancel()
        self.session.on_client_status(self.uid, ["mika", "luna"], [])

    async def run(self, seconds: float, step: float = 0.25) -> None:
        for _ in range(int(seconds / step)):
            self.clock.t += step
            await self.session.push(self.session.tick())
            for _ in range(3):
                await asyncio.sleep(0)

    def audio_payloads(self) -> list[dict]:
        return [m for m in self.sent if m.get("type") == "audio"]

    def chat(self, user: str, text: str) -> bool:
        return self.session.observe_viewer_message(
            LiveMessage(
                "youtube", f"m-{self.clock.t}", user, text, self.clock.t, author_id=user
            )
        )


def setup_function() -> None:
    usage.reset()
    runtime.paused = False


def test_thirty_idle_minutes_make_no_llm_or_tts_requests():
    async def scenario():
        sim = Sim()
        await sim.connect()
        await sim.run(THIRTY_MINUTES)
        return sim

    sim = asyncio.run(scenario())
    snapshot = usage.snapshot()
    assert snapshot["llm_requests"] == 0
    assert snapshot["tts_requests"] == 0
    assert snapshot["viewer_triggered_interactions"] == 0
    assert sim.tts.calls == []
    assert sim.audio_payloads() == []
    assert not sim.session.show.engine.playing
    assert sim.session.show.engine.games_started == 0  # never auto-starts
    assert not sim.session.speech_allowed()


def test_game_started_by_viewer_ends_by_itself_and_spending_stops():
    async def scenario():
        sim = Sim()
        await sim.connect()
        assert sim.chat("@ana", "play trivia") is True  # a viewer asks, then leaves
        started = sim.clock.t
        await sim.run(5)
        assert sim.session.show.engine.playing
        calls_at = {}
        for minute in range(1, 31):
            await sim.run(60, step=0.5)
            calls_at[minute] = len(sim.tts.calls)
        return sim, started, calls_at

    sim, started, calls_at = asyncio.run(scenario())
    snapshot = usage.snapshot()
    assert snapshot["llm_requests"] == 0  # game lines are templates, never the LLM
    assert snapshot["viewer_triggered_interactions"] == 1
    # Speech only while the viewer was recently active (120 s window by default).
    window_minutes = int(sim.session.room.speech_window_seconds // 60) + 1
    assert calls_at[1] >= 1, (
        "the viewer-triggered game should speak while the viewer is around"
    )
    assert calls_at[window_minutes] == calls_at[30]
    assert snapshot["tts_requests"] == len(sim.tts.calls) == calls_at[30]
    # The game paused, then ended on its own, and the board was removed.
    assert not sim.session.show.engine.playing
    board_ops = [
        op
        for m in sim.sent
        if m.get("type") == "vr-room-update"
        for op in m["ops"]
        if op["op"] == "board"
    ]
    assert board_ops and board_ops[-1]["view"] is None
    events = [e["event"] for e in sim.session.bus.recent(300)]
    assert "GAME_PAUSED" in events and "GAME_STOPPED" in events
    stopped = [e for e in sim.session.bus.recent(300) if e["event"] == "GAME_STOPPED"]
    assert stopped[-1]["reason"] == "inactive"
    assert sim.session.show.engine.games_started == 1


def test_game_lines_without_viewers_are_captions_not_tts():
    async def scenario():
        sim = Sim()
        await sim.connect()
        sim.chat("@ana", "play trivia")
        # Pretend the viewer chatted long ago: speech is no longer allowed.
        sim.session.state.last_viewer_at = sim.clock.t - 10_000
        sim.session.show.engine.notify_viewer_activity(
            sim.clock.t
        )  # keep the game running
        await sim.run(40, step=0.5)
        return sim

    sim = asyncio.run(scenario())
    assert sim.tts.calls == []
    assert usage.snapshot()["tts_requests"] == 0
    lines = [
        op
        for m in sim.sent
        if m.get("type") == "vr-room-update"
        for op in m["ops"]
        if op["op"] == "line"
    ]
    assert lines, "game lines should be shown as captions"
    assert sim.session.show.lines_shown_silently >= 1


class CountingProvider:
    """Stands in for the WebSocket handler: counts every reply the loop starts."""

    def __init__(self):
        self.replies = 0

    def has_connected_clients(self) -> bool:
        return True

    def is_idle(self) -> bool:
        return True

    async def process_youtube_live_message(self, message, **kwargs) -> bool:
        self.replies += 1
        usage.record_llm("reply")
        return True


def test_reply_loop_sleeps_without_viewer_messages_even_with_idle_banter_enabled():
    config = SimpleNamespace(
        youtube_live_enabled=True,
        chat_source="api",
        api_key="",
        channel_id="",
        video_id=None,
        prefer_stream_list=True,
        max_buffer_messages=100,
        message_buffer_seconds=180,
        same_user_cooldown_seconds=90,
        response_cooldown_seconds=1,
        selector_max_messages=12,
        idle_banter_enabled=True,  # a stray old setting must not spend credits
        idle_banter_delay_seconds=0,
        discovery_retry_seconds=10,
    )
    provider = CountingProvider()
    service = YouTubeLiveChatService(config, None, provider)
    service._last_message_seen_at = 0  # "quiet for a very long time"
    iterations = {"n": 0}

    async def fake_wait(timeout: float) -> None:
        iterations["n"] += 1
        if iterations["n"] >= THIRTY_MINUTES:  # one iteration per simulated second
            service._running = False
        await asyncio.sleep(0)

    service._wait_for_work = fake_wait
    service._running = True
    asyncio.run(asyncio.wait_for(service._response_loop(), timeout=60))
    assert iterations["n"] == THIRTY_MINUTES
    assert provider.replies == 0
    assert usage.snapshot()["llm_requests"] == 0
    assert usage.snapshot()["tts_requests"] == 0


def test_messages_consumed_by_the_room_never_reach_reply_selection():
    from datetime import datetime, timezone

    from open_llm_vtuber.live.youtube_live import YouTubeChatMessage

    class Observer(CountingProvider):
        def __init__(self):
            super().__init__()
            self.seen = []

        def observe_live_message(self, message) -> bool:
            self.seen.append(message.text)
            return message.text == "play trivia"

    config = SimpleNamespace(
        youtube_live_enabled=True,
        chat_source="api",
        api_key="",
        channel_id="",
        video_id=None,
        prefer_stream_list=True,
        max_buffer_messages=100,
        message_buffer_seconds=180,
        same_user_cooldown_seconds=90,
        response_cooldown_seconds=1,
        selector_max_messages=12,
        idle_banter_enabled=False,
        idle_banter_delay_seconds=0,
        discovery_retry_seconds=10,
    )
    provider = Observer()
    service = YouTubeLiveChatService(config, None, provider)

    def msg(mid, text):
        return YouTubeChatMessage(
            mid, "UCviewer" + mid, "@viewer", text, datetime.now(timezone.utc)
        )

    service._accept(msg("1", "play trivia"), "message")
    service._accept(msg("2", "Mika what is your favorite food?"), "message")
    assert provider.seen == ["play trivia", "Mika what is your favorite food?"]
    eligible = [m.text for m in service.buffer.get_eligible(10)]
    assert eligible == ["Mika what is your favorite food?"]
