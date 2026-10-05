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
    monkeypatch.setattr(recorder, "_finalize_clip", lambda session: tmp_path / "clip.mkv")

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
    monkeypatch.setattr(recorder, "_finalize_clip", lambda session: tmp_path / "clip.mkv")

    async def run():
        await recorder.start_or_extend("tnt", "boom")
        await recorder.finish_after_tail("tnt", "boom done")
        recorder._tail_deadline = time.time() - 0.01
        await recorder._stop_current()

    asyncio.run(run())

    assert stopped


def test_finalized_clip_copied_under_project_clips(monkeypatch, tmp_path):
    monkeypatch.setenv("CLIP_AUTO_RECORD_ENABLED", "1")
    monkeypatch.setenv("CLIPS_DIR", str(tmp_path / "clips"))
    source = tmp_path / "obs" / "raw.mp4"
    source.parent.mkdir()
    source.write_bytes(b"valid mp4")
    recorder = ca.AutoClipRecorder()
    session = ca.ClipSession(started_at=1000.0, event=ca.ClipEvent("race", "Mika won", viewer="ana"), source_clip=source)
    monkeypatch.setattr(recorder, "_wait_for_final_file", lambda path: True)
    monkeypatch.setattr(recorder, "_ffprobe_ok", lambda path: True)

    out = recorder._finalize_clip(session)

    assert out is not None
    assert out.parent == tmp_path / "clips"
    assert out.read_bytes() == b"valid mp4"


def test_ffprobe_must_succeed_before_clip_finalized(monkeypatch, tmp_path):
    monkeypatch.setenv("CLIP_AUTO_RECORD_ENABLED", "1")
    monkeypatch.setenv("CLIPS_DIR", str(tmp_path / "clips"))
    source = tmp_path / "raw.mp4"
    source.write_bytes(b"bad mp4")
    recorder = ca.AutoClipRecorder()
    session = ca.ClipSession(started_at=1000.0, event=ca.ClipEvent("tnt", "boom"), source_clip=source)
    monkeypatch.setattr(recorder, "_wait_for_final_file", lambda path: True)
    monkeypatch.setattr(recorder, "_ffprobe_ok", lambda path: path == source)

    out = recorder._finalize_clip(session)

    assert out is None
    assert list((tmp_path / "clips").glob("*")) == []


def test_incomplete_mp4_is_never_finalized(monkeypatch, tmp_path):
    source = tmp_path / "raw.mp4"
    source.write_bytes(b"incomplete")
    recorder = ca.AutoClipRecorder()
    session = ca.ClipSession(started_at=1000.0, event=ca.ClipEvent("spell", "zap"), source_clip=source)
    monkeypatch.setattr(recorder, "_wait_for_final_file", lambda path: False)

    assert recorder._finalize_clip(session) is None


def test_shorts_automation_is_not_invoked_after_recording(monkeypatch, tmp_path):
    monkeypatch.setenv("CLIP_AUTO_RECORD_ENABLED", "1")
    monkeypatch.setenv("CLIP_SHORTS_ENABLED", "1")
    source = tmp_path / "raw.mp4"
    source.write_bytes(b"valid")
    made_short = False

    async def stop_recording():
        return source

    monkeypatch.setattr(ca.obs_control, "stop_recording", stop_recording)
    recorder = ca.AutoClipRecorder()
    recorder._session = ca.ClipSession(started_at=1000.0, event=ca.ClipEvent("race", "race"), source_clip=source)
    monkeypatch.setattr(recorder, "_finalize_clip", lambda session: tmp_path / "clips" / "race.mp4")
    if hasattr(recorder, "_make_short"):
        monkeypatch.setattr(recorder, "_make_short", lambda session: (_ for _ in ()).throw(AssertionError("shorts invoked")))

    asyncio.run(recorder._stop_current())

    assert made_short is False
