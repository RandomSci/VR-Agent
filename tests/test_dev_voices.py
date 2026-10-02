"""DEV must speak with the real voices, built the same way as the livestream.

Regression: the DEV server used to replace every voice with a test tone, so
Mika and Luna sounded like a vibration instead of Ana and Maisie.

Nothing here touches the network: Edge TTS only connects when it speaks, and
these tests only build the engines.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from open_llm_vtuber.config_manager.tts import EdgeTTSConfig, TTSConfig  # noqa: E402
from tests.harness.fake_tts import FakeTTS  # noqa: E402
from tests.harness.full_dev_server import create_app  # noqa: E402

ANA = "en-US-AnaNeural"
MAISIE = "en-GB-MaisieNeural"


def edge_context(voice: str = ANA):
    tts = TTSConfig(tts_model="edge_tts", edge_tts=EdgeTTSConfig(voice=voice))
    return SimpleNamespace(character_config=SimpleNamespace(tts_config=tts))


def engine_of(app, character_id):
    engine = app.state.session.voices.engine(character_id)
    return getattr(engine, "primary", engine)


def test_dev_uses_real_voices_by_default():
    app = create_app(ROOT, context=edge_context())
    mika = engine_of(app, "mika")
    luna = engine_of(app, "luna")
    assert not isinstance(mika, FakeTTS)
    assert not isinstance(luna, FakeTTS)
    assert type(mika).__module__.endswith("edge_tts")
    assert type(luna).__module__.endswith("edge_tts")
    assert mika.voice == ANA  # inherits conf.yaml
    assert luna.voice == MAISIE  # her own voice from luna.yaml


def test_mika_follows_conf_yaml_voice():
    app = create_app(ROOT, context=edge_context("en-US-JennyNeural"))
    assert engine_of(app, "mika").voice == "en-US-JennyNeural"
    assert engine_of(app, "luna").voice == MAISIE


def test_fake_voices_only_when_asked():
    tone = FakeTTS()
    app = create_app(ROOT, context=edge_context(), tts=tone)
    assert app.state.session.voices.engine("mika") is tone
    assert app.state.session.voices.engine("luna") is tone


def test_no_conf_yaml_never_falls_back_to_the_tone():
    app = create_app(ROOT, context=SimpleNamespace(character_config=None))
    assert not isinstance(app.state.session.voices.engine("mika"), FakeTTS)
    assert not isinstance(app.state.session.voices.engine("luna"), FakeTTS)
