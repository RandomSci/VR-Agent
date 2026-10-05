from pathlib import Path

import pytest


stream_audio = pytest.importorskip("src.open_llm_vtuber.utils.stream_audio")


ROOT = Path(__file__).resolve().parents[1]


def test_audio_payload_separates_protocol_type_and_mime(tmp_path):
    generators = pytest.importorskip("pydub.generators")
    audio = generators.Sine(440).to_audio_segment(duration=200).apply_gain(-6)
    path = tmp_path / "voice.wav"
    audio.export(path, format="wav")

    payload = stream_audio.prepare_audio_payload(str(path))

    assert payload["type"] == "audio"
    assert payload["mime"] == "audio/wav"
    assert payload["audio"]
    assert payload["audio_level"]["rms"] > 0
    assert payload["audio_level"]["peak"] > 0
    assert payload["audio_level"]["silent"] is False


def test_silent_audio_payload_keeps_protocol_type():
    payload = stream_audio.prepare_audio_payload(None)

    assert payload["type"] == "audio"
    assert payload["mime"] is None
    assert payload["audio"] is None


def test_frontend_audio_diagnostics_do_not_overwrite_message_type():
    for rel in ("frontend/vr-agent/room.js", "frontend/vr-agent/teaching-dev-room.js"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert 'type: "audio/wav"' not in text
        assert 'mime: item.mime' in text
        assert 'type: "vr-room-audio-diagnostic"' in text
