"""Luna speaks with ElevenLabs and falls back to Edge TTS if ElevenLabs fails."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from open_llm_vtuber.room.profiles import VoiceSpec, load_room
from open_llm_vtuber.room.session import RoomSession
from open_llm_vtuber.room.speech import CharacterVoices, FallbackTTS

ROOT = Path(__file__).resolve().parents[1]


class FakeEngine:
    def __init__(self, name, tmp_path, result="file", **kwargs):
        self.name = name
        self.kwargs = kwargs
        self.tmp_path = tmp_path
        self.result = result
        self.calls = 0

    async def async_generate_audio(self, text, file_name_no_ext=None):
        self.calls += 1
        if self.result == "raise":
            raise RuntimeError("quota exceeded")
        if self.result == "none":
            return None
        path = self.tmp_path / f"{self.name}-{self.calls}.mp3"
        path.write_bytes(b"audio")
        return str(path)

    def remove_file(self, filepath, verbose=True):
        pass


def test_luna_yaml_uses_elevenlabs_with_an_edge_fallback():
    room = load_room(ROOT / "room", ROOT)
    luna = room.get("luna")
    assert luna.voice.tts_model == "elevenlabs_tts"
    assert luna.voice.settings["voice_id"] == "ExVVn0SQueMnWIWTbm7F"
    assert luna.voice.fallback.tts_model == "edge_tts"
    assert luna.voice.fallback.settings["voice"] == "en-US-AnaNeural"


def test_fallback_is_not_nested():
    spec = VoiceSpec.parse(
        {
            "tts_model": "elevenlabs_tts",
            "fallback": {
                "tts_model": "edge_tts",
                "fallback": {"tts_model": "pyttsx3_tts"},
            },
        }
    )
    assert spec.fallback.tts_model == "edge_tts"
    assert spec.fallback.fallback is None


def _voices(tmp_path, primary_result="file", primary_raises_on_create=False):
    room = load_room(ROOT / "room", ROOT)
    session = RoomSession(room)
    built: dict[str, FakeEngine] = {}

    def factory(model, **kwargs):
        if model == "elevenlabs_tts" and primary_raises_on_create:
            raise ValueError("missing api_key")
        result = primary_result if model == "elevenlabs_tts" else "file"
        built[model] = FakeEngine(model, tmp_path, result, **kwargs)
        return built[model]

    voices = CharacterVoices(session, factory=factory)
    conf_block = SimpleNamespace(
        model_dump=lambda: {
            "api_key": "sk-test",
            "voice_id": "mika-voice",
            "model_id": "eleven_flash_v2_5",
        }
    )
    voices.base_source = lambda: (
        SimpleNamespace(tts_model="elevenlabs_tts", elevenlabs_tts=conf_block),
        object(),
    )
    return voices, built


def test_luna_inherits_the_elevenlabs_key_and_model_but_keeps_her_voice(tmp_path):
    voices, built = _voices(tmp_path)
    engine = voices.engine("luna")
    assert isinstance(engine, FallbackTTS)
    assert built["elevenlabs_tts"].kwargs["api_key"] == "sk-test"
    assert built["elevenlabs_tts"].kwargs["model_id"] == "eleven_flash_v2_5"
    assert built["elevenlabs_tts"].kwargs["voice_id"] == "ExVVn0SQueMnWIWTbm7F"
    assert engine._vr_usage_source == "room:luna"


def test_elevenlabs_is_used_when_it_works(tmp_path):
    voices, built = _voices(tmp_path)
    path = asyncio.run(voices.engine("luna").async_generate_audio("hi"))
    assert "elevenlabs_tts" in path
    assert built["edge_tts"].calls == 0


def test_edge_speaks_when_elevenlabs_raises_or_returns_nothing(tmp_path):
    for result in ("raise", "none"):
        voices, built = _voices(tmp_path, primary_result=result)
        engine = voices.engine("luna")
        path = asyncio.run(engine.async_generate_audio("hi"))
        assert "edge_tts" in path
        assert engine.fallbacks_used == 1


def test_edge_is_used_alone_when_elevenlabs_cannot_be_created(tmp_path):
    voices, built = _voices(tmp_path, primary_raises_on_create=True)
    engine = voices.engine("luna")
    assert engine is built["edge_tts"]


def test_voice_ids_skip_a_voice_the_other_character_already_uses(tmp_path):
    voices, built = _voices(tmp_path)
    block = SimpleNamespace(
        model_dump=lambda: {"api_key": "sk-test", "voice_id": "taken-voice"}
    )
    voices.base_source = lambda: (
        SimpleNamespace(tts_model="elevenlabs_tts", elevenlabs_tts=block),
        object(),
    )
    spec = VoiceSpec.parse(
        {
            "tts_model": "elevenlabs_tts",
            "settings": {"voice_ids": ["taken-voice", "free-voice"]},
        }
    )
    voices._build(spec, "luna")
    assert built["elevenlabs_tts"].kwargs["voice_id"] == "free-voice"
    assert "voice_ids" not in built["elevenlabs_tts"].kwargs


def test_a_primary_voice_that_keeps_failing_rests(tmp_path):
    now = [0.0]
    primary = FakeEngine("elevenlabs_tts", tmp_path, "raise")
    backup = FakeEngine("edge_tts", tmp_path)
    engine = FallbackTTS(primary, backup, "luna", clock=lambda: now[0])
    for _ in range(5):
        asyncio.run(engine.async_generate_audio("hi"))
    assert primary.calls == FallbackTTS.FAILURES_BEFORE_REST
    assert backup.calls == 5 and "quota" in engine.last_error
    now[0] += FallbackTTS.REST_SECONDS + 1
    asyncio.run(engine.async_generate_audio("hi"))
    assert primary.calls == FallbackTTS.FAILURES_BEFORE_REST + 1


def test_elevenlabs_without_an_api_key_goes_straight_to_the_fallback(tmp_path):
    voices, built = _voices(tmp_path)
    empty = SimpleNamespace(model_dump=lambda: {"api_key": "", "model_id": "x"})
    voices.base_source = lambda: (
        SimpleNamespace(tts_model="edge_tts", elevenlabs_tts=empty),
        object(),
    )
    engine = voices.engine("luna")
    assert engine is built["edge_tts"]  # no failing ElevenLabs call before each line
    assert "elevenlabs_tts" not in built
    assert "api_key" in voices.errors.get("luna", "")


def test_voice_report_shows_engine_voice_and_settings_without_keys(tmp_path):
    voices, built = _voices(tmp_path)
    built_engine = voices.engine("luna")
    built_engine.primary.voice_id = "ExVVn0SQueMnWIWTbm7F"
    built_engine.primary.model_id = "eleven_flash_v2_5"
    built_engine.primary.api_key = "sk-secret"
    report = voices.report()
    luna = report["luna"]
    assert luna["source"] == "character yaml"
    assert luna["voice_id"] == "ExVVn0SQueMnWIWTbm7F"
    assert luna["model_id"] == "eleven_flash_v2_5"
    assert "fallback" in luna and luna["fallback_lines"] == 0
    assert "sk-secret" not in str(report)
    assert report["mika"]["source"] == "conf.yaml"
