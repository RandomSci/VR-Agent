"""A light service context for the DEV harness.

``RoomRuntimes`` only needs three things from the big ServiceContext:
``character_config.agent_config``, ``character_config.tts_preprocessor_config``
and ``system_config``. Building the full context would also spin up ASR,
which DEV does not want (it never listens to a microphone).

So this reads the real ``conf.yaml`` and hands over just those pieces. The LLM
is the real one from conf.yaml, so coding in public behaves exactly like the
livestream. With no API key configured the server still boots and serves the
Stage; it just says so plainly in the log, and the characters stay quiet.

Voices are real too. ``build_conf_tts`` makes conf.yaml's TTS engine the same
way ``ServiceContext.init_tts`` does on the livestream, so Mika (``inherit``)
speaks with conf.yaml's voice and Luna builds her own from luna.yaml.

The automated tests do not need an LLM at all: they stub the director's
turn runner and semantic router, which is where the logic under test lives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from loguru import logger


def load_dev_context(root: Path, config_path: Optional[Path] = None) -> Any:
    """Real conf.yaml, no ASR and no TTS engine. None when it cannot be read."""
    from open_llm_vtuber.config_manager.utils import read_yaml, validate_config

    path = Path(config_path or root / "conf.yaml")
    if not path.is_file():
        logger.warning(f"DEV: {path.name} not found; characters will not speak")
        return None
    try:
        config = validate_config(read_yaml(str(path)))
    except Exception as exc:
        logger.error(f"DEV: conf.yaml unusable ({exc}); characters will not speak")
        return None
    return config


def build_conf_tts(context: Any) -> tuple[Any, Any]:
    """conf.yaml's TTS config and engine, built exactly like the livestream.

    Returns (tts_config, engine). Raises with a plain reason when it cannot be
    built, so the server can say why instead of speaking with a placeholder.
    """
    if context is None:
        raise RuntimeError("no conf.yaml")
    tts_config = context.character_config.tts_config
    model = str(getattr(tts_config, "tts_model", "") or "")
    if not model:
        raise RuntimeError("conf.yaml has no tts_model")
    block = getattr(tts_config, model.lower(), None)
    if block is None:
        raise RuntimeError(f"conf.yaml has no {model} block under tts_config")
    from open_llm_vtuber.tts.tts_factory import TTSFactory

    engine = TTSFactory.get_tts_engine(model, **block.model_dump())
    if engine is None:
        raise RuntimeError(f"{model} could not be created")
    try:
        engine._vr_usage_source = "conf"
    except Exception:
        pass
    return tts_config, engine


def describe_tts(tts_config: Any) -> str:
    try:
        model = tts_config.tts_model
        block = getattr(tts_config, model.lower(), None)
        settings = block.model_dump() if block is not None else {}
        voice = settings.get("voice") or settings.get("voice_id") or "?"
        return f"{model} ({voice})"
    except Exception:
        return "unknown"


def has_llm_key(context: Any) -> bool:
    """True when the configured LLM actually has somewhere to connect."""
    if context is None:
        return False
    try:
        agent = context.character_config.agent_config
        provider = (
            agent.agent_settings.model_dump().get("basic_memory_agent") or {}
        ).get("llm_provider")
        settings = agent.llm_configs.model_dump().get(provider) or {}
    except Exception:
        return False
    key = str(settings.get("llm_api_key") or "").strip()
    base = str(settings.get("base_url") or "").strip()
    # An unexpanded ${VAR}, an empty key and the shipped placeholder all mean
    # "not configured". A local base_url (Ollama, LM Studio) needs no key.
    placeholder = key.startswith("${") or key in ("", "YOUR API KEY HERE")
    local = "localhost" in base or "127.0.0.1" in base
    return local or not placeholder


def describe_llm(context: Any) -> str:
    if context is None:
        return "none (no conf.yaml)"
    try:
        agent = context.character_config.agent_config
        provider = (
            agent.agent_settings.model_dump().get("basic_memory_agent") or {}
        ).get("llm_provider")
        settings = agent.llm_configs.model_dump().get(provider) or {}
        model = settings.get("model") or "?"
        return f"{provider} ({model})"
    except Exception:
        return "unknown"
