import asyncio
from types import SimpleNamespace as NS

from src.open_llm_vtuber.publishing import obs_control, settings, youtube


class FakeClient:
    def __init__(self, upcoming):
        self.upcoming = upcoming
        self.calls = []
        self.live = False
        self.active = []  # other active broadcasts (old ones, previews)

    def find_active_broadcast_video_id(self):
        return "vid" if self.live else ""

    def active_broadcasts(self):
        return list(self.active) + ([{"id": "vid", "life": "live", "stream": "s1"}] if self.live else [])

    def stream_for_key(self, key):
        return {"id": "s1", "status": "active"}

    def upcoming_broadcasts(self):
        return self.upcoming

    def broadcast_status(self, bid):
        if self.live and any(c[:3] == ("transition", bid, "live") for c in self.calls):
            return "live"
        if any(c[:3] == ("transition", bid, "testing") for c in self.calls):
            return "testing"
        for b in self.upcoming + self.active:
            if b["id"] == bid:
                return b["life"]
        return "ready" if bid == "new1" else ""

    def delete_broadcast(self, bid):
        self.calls.append(("delete", bid))

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


def test_an_old_active_broadcast_on_another_stream_does_not_count(monkeypatch):
    fake = FakeClient([{"id": "b1", "life": "ready", "stream": "s1", "monitor": False}])
    fake.active = [{"id": "old", "life": "live", "stream": "other"}]
    assert _run(monkeypatch, fake) is True
    assert ("transition", "b1", "live") in fake.calls  # ours was still started


def test_a_broadcast_stuck_in_its_preview_goes_live(monkeypatch):
    fake = FakeClient([])
    fake.active = [{"id": "b2", "life": "testing", "stream": "s1"}]

    def transition(bid, status):
        fake.calls.append(("transition", bid, status))
        if status == "live":
            fake.active = []
            fake.live = True

    fake.transition = transition
    assert _run(monkeypatch, fake) is True
    assert ("transition", "b2", "live") in fake.calls and ("create",) not in fake.calls


def test_advanced_output_bitrate_is_written(tmp_path, monkeypatch):
    import json

    (tmp_path / "basic.ini").write_text("[Video]\nFPSCommon=60\n")
    (tmp_path / "streamEncoder.json").write_text(json.dumps({"bitrate": 12000, "keyint_sec": 2}))
    monkeypatch.setattr(obs_control, "_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(obs_control, "_stream_quality", lambda: (30, 6000))
    obs_control._write_stream_quality()
    data = json.loads((tmp_path / "streamEncoder.json").read_text())
    assert data["bitrate"] == 6000 and data["keyint_sec"] == 2 and data["rate_control"] == "CBR"
