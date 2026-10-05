import asyncio
import time
from pathlib import Path

from src.open_llm_vtuber.publishing import clip_automation as ca


def test_clip_recorder_starts_once_and_overlap_extends(monkeypatch, tmp_path):
    monkeypatch.setenv("CLIP_AUTO_RECORD_ENABLED", "1")
    monkeypatch.setenv("CLIP_SHORTS_ENABLED", "0")
    starts = 0

    async def start_recording():
        nonlocal starts
        starts += 1
        return True

    async def stop_recording():
        return tmp_path / "clip.mkv"

    monkeypatch.setattr(ca.obs_control, "start_recording", start_recording)
    monkeypatch.setattr(ca.obs_control, "stop_recording", stop_recording)
    recorder = ca.AutoClipRecorder()

    async def run():
        assert await recorder.start_or_extend("race", "race one")
        first_deadline = recorder._tail_deadline
        assert await recorder.start_or_extend("race", "race two")
        event_count = len(recorder._session.events) if recorder._session else 0
        await recorder._stop_current()
        return first_deadline, event_count

    first_deadline, event_count = asyncio.run(run())

    assert starts == 1
    assert event_count == 2
    assert recorder._session is None
    assert recorder._tail_deadline >= first_deadline


def test_clip_recorder_obs_error_does_not_raise(monkeypatch):
    monkeypatch.setenv("CLIP_AUTO_RECORD_ENABLED", "1")

    async def start_recording():
        raise RuntimeError("OBS down")

    monkeypatch.setattr(ca.obs_control, "start_recording", start_recording)
    recorder = ca.AutoClipRecorder()

    async def run():
        return await recorder.start_or_extend("race", "race")

    assert asyncio.run(run()) is False
    assert recorder._session is None


def test_finish_tail_stops_later(monkeypatch, tmp_path):
    monkeypatch.setenv("CLIP_AUTO_RECORD_ENABLED", "1")
    monkeypatch.setenv("CLIP_SHORTS_ENABLED", "0")
    stopped = False

    async def start_recording():
        return True

    async def stop_recording():
        nonlocal stopped
        stopped = True
        return Path(tmp_path / "clip.mkv")

    monkeypatch.setattr(ca.obs_control, "start_recording", start_recording)
    monkeypatch.setattr(ca.obs_control, "stop_recording", stop_recording)
    recorder = ca.AutoClipRecorder()

    async def run():
        await recorder.start_or_extend("tnt", "boom")
        await recorder.finish_after_tail("tnt", "boom done")
        recorder._tail_deadline = time.time() - 0.01
        await recorder._stop_current()

    asyncio.run(run())

    assert stopped
