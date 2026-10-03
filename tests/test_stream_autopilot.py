import asyncio
from types import SimpleNamespace

from open_llm_vtuber.publishing.stream_autopilot import (
    CHANNEL_DESCRIPTION,
    StreamAutopilot,
    stream_title,
)


def test_titles_fit_youtube():
    title = stream_title("Math with Python", 7, 9, "A very long lesson name " * 5)
    assert len(title) <= 100 and "Lesson 7" in title
    assert len(CHANNEL_DESCRIPTION) <= 1000


def test_end_stream_says_goodbye_then_stops(monkeypatch):
    monkeypatch.setenv("OBS_WEBSOCKET_PASSWORD", "")
    settings = SimpleNamespace(
        youtube_enabled=True, youtube_chat_enabled=False, dry_run=True, youtube_ready=False
    )
    pilot = StreamAutopilot(SimpleNamespace(settings=settings))
    calls = []

    async def goodbye(reason):
        calls.append(reason)

    pilot.goodbye = goodbye
    pilot.shutdown = lambda: calls.append("shutdown")
    asyncio.run(pilot.end_stream("limit"))
    asyncio.run(pilot.end_stream("limit"))  # only once
    assert calls == ["limit", "shutdown"]


def test_thumbnail_uploaded_once_per_video(tmp_path):
    image = tmp_path / "t.jpg"
    image.write_bytes(b"jpg")
    uploads = []

    class Client:
        def set_thumbnail(self, video_id, data, mime):
            uploads.append((video_id, data, mime))

    settings = SimpleNamespace(youtube_enabled=True, dry_run=False, youtube_ready=True)
    publisher = SimpleNamespace(
        settings=settings, _youtube_factory=Client, _video_id=lambda client: "vid1"
    )
    pilot = StreamAutopilot(publisher)
    asyncio.run(pilot.set_thumbnail(str(image)))
    asyncio.run(pilot.set_thumbnail(str(image)))
    assert uploads == [("vid1", b"jpg", "image/jpeg")]


def test_playlist_made_once_and_video_added_once(tmp_path, monkeypatch):
    from open_llm_vtuber.publishing import stream_autopilot as sa

    monkeypatch.setattr(sa, "PLAYLIST_FILE", tmp_path / "playlists.json")
    calls = []

    class Client:
        def find_playlist(self, title):
            calls.append("find")
            return ""

        def create_playlist(self, title, description=""):
            calls.append("create")
            return "PL1"

        def playlist_has(self, playlist_id, video_id):
            return False

        def add_to_playlist(self, playlist_id, video_id):
            calls.append(("add", playlist_id, video_id))

    settings = SimpleNamespace(youtube_enabled=True, dry_run=False, youtube_ready=True)
    publisher = SimpleNamespace(settings=settings, _youtube_factory=Client, _video_id=lambda c: "vid9")
    pilot = StreamAutopilot(publisher)
    asyncio.run(pilot.add_to_playlist("minecraft"))
    asyncio.run(pilot.add_to_playlist("minecraft"))
    assert calls == ["find", "create", ("add", "PL1", "vid9")]
    assert "PL1" in (tmp_path / "playlists.json").read_text()
