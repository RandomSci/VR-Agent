import asyncio
from types import SimpleNamespace as NS

from src.open_llm_vtuber.publishing import obs_control, settings, youtube


class FakeClient:
    def __init__(self, upcoming):
        self.upcoming = upcoming
        self.calls = []
        self.live = False

    def find_active_broadcast_video_id(self):
        return "vid" if self.live else ""

    def stream_for_key(self, key):
        return {"id": "s1", "status": "active"}

    def upcoming_broadcasts(self):
        return self.upcoming

    def transition(self, bid, status):
        self.calls.append(("transition", bid, status))
        if status == "live":
            self.live = True

    def create_broadcast(self, title, description=""):
        self.calls.append(("create",))
        return "new1"

    def bind(self, bid, sid):
        self.calls.append(("bind", bid, sid))
        self.live = True


def _run(monkeypatch, fake):
    monkeypatch.setattr(youtube, "YouTubeClient", lambda *a, **k: fake)
    monkeypatch.setattr(
        settings.PublishSettings, "from_env",
        classmethod(lambda cls, read_dotenv=False: NS(youtube_ready=True, youtube_client_id="i",
                                                       youtube_client_secret="s", youtube_refresh_token="r")),
    )
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: real_sleep(0))
    return asyncio.run(obs_control.ensure_youtube_live("key", timeout=5))


def test_waiting_broadcast_is_started(monkeypatch):
    fake = FakeClient([{"id": "b1", "life": "ready", "stream": "s1", "monitor": False}])
    assert _run(monkeypatch, fake) is True
    assert ("transition", "b1", "live") in fake.calls


def test_monitor_goes_through_testing(monkeypatch):
    fake = FakeClient([{"id": "b1", "life": "ready", "stream": "s1", "monitor": True}])
    _run(monkeypatch, fake)
    assert fake.calls[:2] == [("transition", "b1", "testing"), ("transition", "b1", "live")]


def test_no_broadcast_makes_one(monkeypatch):
    fake = FakeClient([])
    assert _run(monkeypatch, fake) is True
    assert ("bind", "new1", "s1") in fake.calls
