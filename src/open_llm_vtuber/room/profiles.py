"""Room configuration and character profiles.

Everything that makes a character unique lives in YAML, not code:

    room/room.yaml             cast, relationship, director, camera, ambient
    room/characters/<id>.yaml  one file per character

Profiles are validated when loaded. A character whose Live2D model is
missing, or whose file is malformed, is dropped with a warning and the rest
of the cast still loads. If fewer than one character survives the room is
disabled and the classic single-character livestream keeps working.

Viewer text never reaches this module.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml
from loguru import logger

from ..vr_agent.capabilities import CharacterCapabilities, load_capabilities

ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
PARAM_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
REACTION_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
OBJECT_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
MAX_CAST = 6
EMOTIONS = (
    "neutral",
    "joy",
    "smirk",
    "surprise",
    "sadness",
    "anger",
    "fear",
    "disgust",
)

DEFAULT_ROOM_DIR = Path("room")


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(low, min(high, number))


@dataclass(frozen=True)
class Layout:
    """Where the character stands on a 16:9 stage, all values are fractions."""

    x: float = 0.5  # horizontal centre of the model
    bottom: float = 1.02  # where the model's bottom edge sits (1.0 is the stage floor)
    height: float = 0.95  # model height as a fraction of stage height
    face_x: float = 0.5  # face position inside the model bounds, for looks and camera
    face_y: float = 0.18

    @classmethod
    def parse(cls, raw: Any) -> "Layout":
        raw = raw if isinstance(raw, dict) else {}
        return cls(
            x=_clamp(raw.get("x"), 0.0, 1.0, cls.x),
            bottom=_clamp(raw.get("bottom"), 0.5, 1.6, cls.bottom),
            height=_clamp(raw.get("height"), 0.2, 2.5, cls.height),
            face_x=_clamp(raw.get("face_x"), 0.0, 1.0, cls.face_x),
            face_y=_clamp(raw.get("face_y"), 0.0, 1.0, cls.face_y),
        )

    def to_frontend(self) -> dict[str, float]:
        return {
            "x": self.x,
            "bottom": self.bottom,
            "height": self.height,
            "face_x": self.face_x,
            "face_y": self.face_y,
        }


@dataclass(frozen=True)
class LookParams:
    """Model parameter ids used to turn head and eyes. Models differ here."""

    angle_x: str = "ParamAngleX"
    angle_y: str = "ParamAngleY"
    angle_z: str = "ParamAngleZ"
    body_x: str = "ParamBodyAngleX"
    eye_x: str = "ParamEyeBallX"
    eye_y: str = "ParamEyeBallY"
    strength: float = 1.0

    @classmethod
    def parse(cls, raw: Any) -> "LookParams":
        raw = raw if isinstance(raw, dict) else {}
        values: dict[str, Any] = {}
        for key in ("angle_x", "angle_y", "angle_z", "body_x", "eye_x", "eye_y"):
            value = raw.get(key, getattr(cls, key))
            if value is None or value == "":
                values[key] = ""
            elif isinstance(value, str) and PARAM_RE.match(value):
                values[key] = value
            else:
                raise ValueError(f"look.{key} is not a valid parameter id")
        values["strength"] = _clamp(raw.get("strength"), 0.0, 1.5, 1.0)
        return cls(**values)

    def param_ids(self) -> list[str]:
        return [
            p
            for p in (
                self.angle_x,
                self.angle_y,
                self.angle_z,
                self.body_x,
                self.eye_x,
                self.eye_y,
            )
            if p
        ]

    def to_frontend(self) -> dict[str, Any]:
        return {
            "angle_x": self.angle_x,
            "angle_y": self.angle_y,
            "angle_z": self.angle_z,
            "body_x": self.body_x,
            "eye_x": self.eye_x,
            "eye_y": self.eye_y,
            "strength": self.strength,
        }


@dataclass(frozen=True)
class VoiceSpec:
    """TTS for one character. ``tts_model`` None means use conf.yaml's TTS."""

    tts_model: Optional[str] = None
    settings: dict[str, Any] = field(default_factory=dict)
    # Used when the primary engine fails or returns no audio, so the character never goes silent.
    fallback: Optional["VoiceSpec"] = None

    @classmethod
    def parse(cls, raw: Any, allow_fallback: bool = True) -> "VoiceSpec":
        if not isinstance(raw, dict):
            return cls()
        model = raw.get("tts_model")
        if model in (None, "", "inherit"):
            return cls()
        if not isinstance(model, str) or not re.match(r"^[a-z0-9_]{2,40}$", model):
            raise ValueError("voice.tts_model is not a valid engine name")
        settings = raw.get("settings") or {}
        if not isinstance(settings, dict):
            raise ValueError("voice.settings must be a mapping")
        fallback = None
        if allow_fallback and raw.get("fallback") is not None:
            fallback = cls.parse(raw.get("fallback"), allow_fallback=False)
            if not fallback.tts_model:
                fallback = None
        return cls(tts_model=model, settings=dict(settings), fallback=fallback)

    def describe(self) -> str:
        if not self.tts_model:
            return "conf.yaml TTS"
        ids = self.settings.get("voice_ids")
        voice = (
            self.settings.get("voice")
            or self.settings.get("voice_id")
            or (ids[0] if isinstance(ids, list) and ids else "")
        )
        return f"{self.tts_model} {voice}".strip()


@dataclass
class CharacterProfile:
    id: str
    name: str
    model: str
    persona: str
    aliases: tuple[str, ...] = ()
    color: str = "#ffffff"
    primary: bool = False
    voice: VoiceSpec = field(default_factory=VoiceSpec)
    layout: Layout = field(default_factory=Layout)
    look: LookParams = field(default_factory=LookParams)
    mouth: str = "ParamMouthOpenY"
    emotions: dict[str, Optional[str]] = field(default_factory=dict)
    topics: tuple[str, ...] = ()
    talkativeness: float = 1.0
    reactions: dict[str, tuple[str, ...]] = field(default_factory=dict)
    game_skill: dict[str, float] = field(default_factory=dict)
    game_lines: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Filled in by load_room once the model is resolved.
    model_info: dict[str, Any] = field(default_factory=dict)
    capabilities: Optional[CharacterCapabilities] = None
    source_path: Optional[Path] = None

    @property
    def names(self) -> tuple[str, ...]:
        """Every name viewers may use for this character, lower case."""
        seen: list[str] = []
        for value in (self.name, self.id, *self.aliases):
            lowered = value.strip().lower()
            if lowered and lowered not in seen:
                seen.append(lowered)
        return tuple(seen)

    def emotion_actions(self) -> list[str]:
        """Registry action for each emotion, by index. Index order is fixed."""
        return [self.emotions.get(e) or "" for e in EMOTIONS]

    def emotion_index_map(self) -> dict[str, int]:
        """Emotion tag -> index, for the agent's [joy] style tags."""
        return {
            e: i
            for i, e in enumerate(EMOTIONS)
            if self.emotions.get(e) or e == "neutral"
        }

    def to_frontend(self) -> dict[str, Any]:
        caps = self.capabilities
        return {
            "id": self.id,
            "name": self.name,
            "color": self.color,
            "primary": self.primary,
            "model_url": str(self.model_info.get("url") or ""),
            "layout": self.layout.to_frontend(),
            "look": self.look.to_frontend(),
            "mouth": self.mouth,
            "emotions": self.emotion_actions(),
            "reactions": {k: list(v) for k, v in self.reactions.items()},
            "capabilities": caps.to_frontend() if caps else None,
        }

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "model": self.model,
            "voice": self.voice.describe(),
            "aliases": list(self.aliases),
            "topics": list(self.topics),
            "primary": self.primary,
            "actions": [a.name for a in self.capabilities.viewer_actions()]
            if self.capabilities
            else [],
        }


@dataclass(frozen=True)
class DirectorSettings:
    max_turns: int = 3
    follow_up_chance: float = 0.25
    silent_reaction_chance: float = 0.9
    fairness_window: int = 6
    failure_threshold: int = 3
    cooldown_seconds: float = 300.0
    context_lines: int = 8
    # There is deliberately no autonomous character-to-character chatter.
    # Without viewer activity the room makes zero LLM and zero TTS requests.

    @classmethod
    def parse(cls, raw: Any) -> "DirectorSettings":
        raw = raw if isinstance(raw, dict) else {}
        if raw.get("autonomous_banter"):
            logger.warning(
                "VR Room: director.autonomous_banter is not supported and is ignored; "
                "the room never talks without viewer activity."
            )
        return cls(
            max_turns=int(_clamp(raw.get("max_turns"), 1, 4, 3)),
            follow_up_chance=_clamp(raw.get("follow_up_chance"), 0, 1, 0.25),
            silent_reaction_chance=_clamp(raw.get("silent_reaction_chance"), 0, 1, 0.9),
            fairness_window=int(_clamp(raw.get("fairness_window"), 2, 30, 6)),
            failure_threshold=int(_clamp(raw.get("failure_threshold"), 1, 10, 3)),
            cooldown_seconds=_clamp(raw.get("cooldown_seconds"), 10, 3600, 300),
            context_lines=int(_clamp(raw.get("context_lines"), 0, 16, 8)),
        )


@dataclass(frozen=True)
class CameraSettings:
    enabled: bool = True
    auto_focus: bool = True
    auto_cooldown_seconds: float = 25.0
    zoom_request_cooldown_seconds: float = 15.0
    zoom_hold_seconds: float = 6.0
    transition_seconds: float = 1.4

    @classmethod
    def parse(cls, raw: Any) -> "CameraSettings":
        raw = raw if isinstance(raw, dict) else {}
        return cls(
            enabled=bool(raw.get("enabled", True)),
            auto_focus=bool(raw.get("auto_focus", True)),
            auto_cooldown_seconds=_clamp(raw.get("auto_cooldown_seconds"), 5, 600, 25),
            zoom_request_cooldown_seconds=_clamp(
                raw.get("zoom_request_cooldown_seconds"), 3, 600, 15
            ),
            zoom_hold_seconds=_clamp(raw.get("zoom_hold_seconds"), 2, 30, 6),
            transition_seconds=_clamp(raw.get("transition_seconds"), 0.4, 4, 1.4),
        )

    def to_frontend(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "transition_seconds": self.transition_seconds}


@dataclass(frozen=True)
class AmbientSettings:
    idle_min_seconds: float = 12.0
    idle_max_seconds: float = 22.0
    glance_min_seconds: float = 25.0
    glance_max_seconds: float = 70.0
    expression_chance: float = 0.15
    # Stage moves (walk, jump, dance). Frontend only, never while a game is on.
    moves_enabled: bool = True
    move_chance: float = 0.45
    walk: bool = True
    jump: bool = True
    dance: bool = True

    @classmethod
    def parse(cls, raw: Any) -> "AmbientSettings":
        raw = raw if isinstance(raw, dict) else {}
        idle_min = _clamp(raw.get("idle_min_seconds"), 4, 300, 12)
        glance_min = _clamp(raw.get("glance_min_seconds"), 5, 600, 25)
        return cls(
            idle_min_seconds=idle_min,
            idle_max_seconds=max(
                idle_min + 1, _clamp(raw.get("idle_max_seconds"), 5, 600, 22)
            ),
            glance_min_seconds=glance_min,
            glance_max_seconds=max(
                glance_min + 1, _clamp(raw.get("glance_max_seconds"), 6, 900, 70)
            ),
            expression_chance=_clamp(raw.get("expression_chance"), 0, 1, 0.15),
            moves_enabled=raw.get("moves_enabled", True) is not False,
            move_chance=_clamp(raw.get("move_chance"), 0, 1, 0.45),
            walk=raw.get("walk", True) is not False,
            jump=raw.get("jump", True) is not False,
            dance=raw.get("dance", True) is not False,
        )

    def to_frontend(self) -> dict[str, Any]:
        return {
            "idle_min_seconds": self.idle_min_seconds,
            "idle_max_seconds": self.idle_max_seconds,
            "glance_min_seconds": self.glance_min_seconds,
            "glance_max_seconds": self.glance_max_seconds,
            "expression_chance": self.expression_chance,
            "moves_enabled": self.moves_enabled,
            "move_chance": self.move_chance,
            "walk": self.walk,
            "jump": self.jump,
            "dance": self.dance,
        }


@dataclass
class RoomConfig:
    enabled: bool = False
    characters: list[CharacterProfile] = field(default_factory=list)
    relationship: str = ""
    background: str = ""
    title: str = "VR AGENT"
    director: DirectorSettings = field(default_factory=DirectorSettings)
    camera: CameraSettings = field(default_factory=CameraSettings)
    ambient: AmbientSettings = field(default_factory=AmbientSettings)
    objects: dict[str, dict[str, float]] = field(default_factory=dict)
    action_objects: dict[str, dict[str, Any]] = field(default_factory=dict)
    sfx_volume: float = 0.35
    music: dict[str, Any] = field(
        default_factory=lambda: {
            "enabled": True,
            "volume": 0.14,
            "game_volume": 0.17,
            "duck_to": 0.35,
            "crossfade_seconds": 1.6,
        }
    )
    speech_window_seconds: float = 120.0
    game_pause_after_seconds: float = 90.0
    game_end_after_seconds: float = 180.0
    problems: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.enabled and bool(self.characters)

    @property
    def primary(self) -> Optional[CharacterProfile]:
        for profile in self.characters:
            if profile.primary:
                return profile
        return self.characters[0] if self.characters else None

    def get(self, character_id: str) -> Optional[CharacterProfile]:
        for profile in self.characters:
            if profile.id == character_id:
                return profile
        return None

    def to_frontend(self) -> dict[str, Any]:
        return {
            "enabled": self.active,
            "title": self.title,
            "background": self.background,
            "characters": [c.to_frontend() for c in self.characters],
            "camera": self.camera.to_frontend(),
            "ambient": self.ambient.to_frontend(),
            "objects": self.objects,
            "sfx_volume": self.sfx_volume,
            "music": dict(self.music),
        }

    def describe(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "active": self.active,
            "characters": [c.describe() for c in self.characters],
            "problems": list(self.problems),
            "director": self.director.__dict__,
        }


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a mapping")
    return data


def _load_model_dict(project_root: Path) -> dict[str, dict[str, Any]]:
    path = project_root / "model_dict.json"
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error(f"VR Room: cannot read model_dict.json: {exc}")
        return {}
    return {str(e.get("name")): e for e in entries if isinstance(e, dict)}


def _model_parameters(
    project_root: Path, model_info: dict[str, Any]
) -> Optional[set[str]]:
    """Parameter ids the model declares (from its cdi3.json), None if unknown."""
    url = str(model_info.get("url") or "")
    if not url.endswith(".model3.json"):
        return None
    model3 = (project_root / url.lstrip("/")).resolve()
    try:
        model3.relative_to(project_root.resolve())
        refs = json.loads(model3.read_text(encoding="utf-8")).get("FileReferences", {})
        cdi = refs.get("DisplayInfo")
        if not cdi:
            return None
        info = json.loads((model3.parent / cdi).read_text(encoding="utf-8"))
        return {str(p.get("Id")) for p in info.get("Parameters", [])}
    except Exception:
        return None


def parse_profile(
    raw: dict[str, Any],
    source: Optional[Path] = None,
) -> CharacterProfile:
    """Validate one character file. Raises ValueError with a readable reason."""
    character_id = str(raw.get("id") or (source.stem if source else "")).strip().lower()
    if not ID_RE.match(character_id):
        raise ValueError(f"id '{character_id}' must be lowercase letters, digits or _")
    name = str(raw.get("name") or "").strip()
    if not name or len(name) > 24 or not re.match(r"^[\w .'-]+$", name):
        raise ValueError("name is required, up to 24 letters")
    model = str(raw.get("model") or "").strip()
    if not model:
        raise ValueError("model is required (a name from model_dict.json)")
    persona = str(raw.get("persona") or "").strip()
    if len(persona) < 20:
        raise ValueError("persona is required")
    if len(persona) > 2500:
        raise ValueError("persona is too long, keep it under 2500 characters")

    aliases = tuple(
        str(a).strip()
        for a in (raw.get("aliases") or [])
        if isinstance(a, (str, int)) and 2 <= len(str(a).strip()) <= 24
    )[:8]
    color = str(raw.get("color") or "#ffffff")
    if not COLOR_RE.match(color):
        color = "#ffffff"
    mouth = str(raw.get("mouth") or "ParamMouthOpenY")
    if not PARAM_RE.match(mouth):
        raise ValueError("mouth is not a valid parameter id")

    emotions_raw = raw.get("emotions") or {}
    if not isinstance(emotions_raw, dict):
        raise ValueError("emotions must be a mapping")
    emotions: dict[str, Optional[str]] = {}
    for emotion, action in emotions_raw.items():
        emotion = str(emotion).lower()
        if emotion not in EMOTIONS:
            continue
        emotions[emotion] = str(action) if action else None

    topics = tuple(
        str(t).strip().lower()
        for t in (raw.get("topics") or [])
        if isinstance(t, str) and 2 <= len(t.strip()) <= 30
    )[:40]
    conversation = (
        raw.get("conversation") if isinstance(raw.get("conversation"), dict) else {}
    )

    reactions: dict[str, tuple[str, ...]] = {}
    for key, actions in (raw.get("reactions") or {}).items():
        if not isinstance(key, str) or not REACTION_RE.match(key):
            continue
        if isinstance(actions, str):
            actions = [actions]
        reactions[key] = tuple(str(a) for a in (actions or []) if isinstance(a, str))[
            :6
        ]

    game = raw.get("game") if isinstance(raw.get("game"), dict) else {}
    skill_raw = game.get("skill") if isinstance(game.get("skill"), dict) else {}
    game_skill = {
        level: _clamp(skill_raw.get(level), 0.0, 1.0, default)
        for level, default in (("easy", 0.8), ("medium", 0.6), ("hard", 0.4))
    }
    game_lines: dict[str, tuple[str, ...]] = {}
    for key, lines in (game.get("lines") or {}).items():
        if not isinstance(key, str) or not REACTION_RE.match(key):
            continue
        if isinstance(lines, str):
            lines = [lines]
        kept = []
        for line in lines or []:
            line = str(line).strip()
            # Only known placeholders are allowed in templates.
            if 1 <= len(line) <= 140 and not re.search(
                r"\{(?!(?:answer|opponent|hand|user)\})", line
            ):
                kept.append(line)
        if kept:
            game_lines[key] = tuple(kept[:10])

    return CharacterProfile(
        id=character_id,
        name=name,
        model=model,
        persona=persona,
        aliases=aliases,
        color=color,
        primary=bool(raw.get("primary", False)),
        voice=VoiceSpec.parse(raw.get("voice")),
        layout=Layout.parse(raw.get("layout")),
        look=LookParams.parse(raw.get("look")),
        mouth=mouth,
        emotions=emotions,
        topics=topics,
        talkativeness=_clamp(conversation.get("talkativeness"), 0.1, 3.0, 1.0),
        reactions=reactions,
        game_skill=game_skill,
        game_lines=game_lines,
        source_path=source,
    )


def load_room(
    room_dir: Path | str = DEFAULT_ROOM_DIR,
    project_root: Path | str = ".",
) -> RoomConfig:
    """Load and validate the room. Never raises; problems disable parts of it."""
    project_root = Path(project_root)
    room_dir = Path(room_dir)
    if not room_dir.is_absolute():
        room_dir = project_root / room_dir
    room = RoomConfig()
    room_file = room_dir / "room.yaml"
    if not room_file.is_file():
        room.problems.append("room/room.yaml not found")
        return room
    try:
        spec = _read_yaml(room_file)
    except Exception as exc:
        room.problems.append(f"room.yaml unreadable: {exc}")
        logger.error(f"VR Room: {room.problems[-1]}")
        return room

    room.enabled = bool(spec.get("enabled", False))
    room.relationship = str(spec.get("relationship") or "").strip()[:1500]
    background = str(spec.get("background") or "").strip()
    if background and not re.match(r"^[\w./ -]{1,120}$", background):
        room.problems.append("background ignored (unexpected characters)")
        background = ""
    room.background = background
    room.title = str(spec.get("title") or "VR AGENT")[:40]
    room.director = DirectorSettings.parse(spec.get("director"))
    room.camera = CameraSettings.parse(spec.get("camera"))
    room.ambient = AmbientSettings.parse(spec.get("ambient"))
    for object_id, box in (spec.get("objects") or {}).items():
        if (
            not isinstance(object_id, str)
            or not OBJECT_RE.match(object_id)
            or not isinstance(box, dict)
        ):
            room.problems.append(f"object '{object_id}' ignored")
            continue
        room.objects[object_id] = {
            "x": _clamp(box.get("x"), 0, 1, 0.5),
            "y": _clamp(box.get("y"), 0, 1, 0.6),
            "width": _clamp(box.get("width"), 0.02, 1, 0.3),
            "height": _clamp(box.get("height"), 0.02, 1, 0.3),
        }
    world = spec.get("world") if isinstance(spec.get("world"), dict) else {}
    for action, rule in (world.get("action_objects") or {}).items():
        if (
            not isinstance(action, str)
            or not REACTION_RE.match(action)
            or not isinstance(rule, dict)
            or not OBJECT_RE.match(str(rule.get("object", "")))
        ):
            room.problems.append(f"world rule for '{action}' ignored")
            continue
        room.action_objects[action] = {
            "object": str(rule["object"]),
            "dx": _clamp(rule.get("dx"), -0.5, 0.5, 0.0),
            "y": _clamp(rule.get("y"), 0, 1, 0.5),
            "seconds": _clamp(rule.get("seconds"), 1, 30, 4),
            "after": _clamp(rule.get("after"), 0, 20, 0),
        }
    sfx = spec.get("sfx") if isinstance(spec.get("sfx"), dict) else {}
    room.sfx_volume = _clamp(sfx.get("volume"), 0, 1, 0.35)
    music = spec.get("music") if isinstance(spec.get("music"), dict) else {}
    room.music = {
        "enabled": bool(music.get("enabled", True)),
        "volume": _clamp(music.get("volume"), 0, 1, 0.14),
        "game_volume": _clamp(music.get("game_volume"), 0, 1, 0.17),
        "duck_to": _clamp(music.get("duck_to"), 0, 1, 0.35),
        "crossfade_seconds": _clamp(music.get("crossfade_seconds"), 0.2, 6, 1.6),
    }
    cost = spec.get("cost") if isinstance(spec.get("cost"), dict) else {}
    room.speech_window_seconds = _clamp(cost.get("speech_window_seconds"), 10, 900, 120)
    games = spec.get("games") if isinstance(spec.get("games"), dict) else {}
    room.game_pause_after_seconds = _clamp(
        games.get("pause_after_seconds"), 15, 1800, 90
    )
    room.game_end_after_seconds = max(
        room.game_pause_after_seconds + 10,
        _clamp(games.get("end_after_seconds"), 30, 3600, 180),
    )

    cast_spec = spec.get("cast") or []
    override = os.environ.get("VR_ROOM_CAST", "").strip()
    if override:  # e.g. VR_ROOM_CAST=mika,luna for Minecraft mode
        cast_spec = [c for c in override.split(",") if c.strip()]
    cast = [str(c).strip().lower() for c in cast_spec if str(c).strip()]
    if not cast:
        room.problems.append("cast is empty")
    models = _load_model_dict(project_root)
    characters_dir = room_dir / "characters"

    for character_id in cast[:MAX_CAST]:
        if not ID_RE.match(character_id):
            room.problems.append(f"cast entry '{character_id}' is not a valid id")
            continue
        path = characters_dir / f"{character_id}.yaml"
        try:
            profile = parse_profile(_read_yaml(path), path)
            if profile.id != character_id:
                raise ValueError(f"id '{profile.id}' does not match file name")
            if any(existing.id == profile.id for existing in room.characters):
                raise ValueError("listed twice")
            model_info = models.get(profile.model)
            if not model_info:
                raise ValueError(f"model '{profile.model}' is not in model_dict.json")
            profile.model_info = dict(model_info)
            declared = _model_parameters(project_root, model_info)
            if declared is not None:
                missing = [
                    p
                    for p in [*profile.look.param_ids(), profile.mouth]
                    if p not in declared
                ]
                if missing:
                    raise ValueError(f"model has no parameter(s) {missing}")
            profile.capabilities = load_capabilities(model_info, project_root)
            unknown = [
                a
                for a in profile.emotions.values()
                if a and not profile.capabilities.get(a)
            ]
            if unknown:
                logger.warning(
                    f"VR Room: {profile.id} emotions point at unknown actions {unknown}; ignored"
                )
                profile.emotions = {
                    e: a
                    for e, a in profile.emotions.items()
                    if not a or profile.capabilities.get(a)
                }
            for key, actions in list(profile.reactions.items()):
                valid = tuple(a for a in actions if profile.capabilities.get(a))
                if len(valid) != len(actions):
                    logger.warning(
                        f"VR Room: {profile.id} reaction '{key}' drops unknown actions "
                        f"{[a for a in actions if a not in valid]}"
                    )
                profile.reactions[key] = valid
        except FileNotFoundError:
            room.problems.append(
                f"{character_id}: room/characters/{character_id}.yaml not found"
            )
            logger.error(f"VR Room: {room.problems[-1]}")
            continue
        except Exception as exc:
            room.problems.append(f"{character_id}: {exc}")
            logger.error(f"VR Room: character '{character_id}' skipped: {exc}")
            continue
        room.characters.append(profile)

    # Exactly one primary character (the ultimate fallback speaker).
    primaries = [c for c in room.characters if c.primary]
    for extra in primaries[1:]:
        extra.primary = False
    if room.characters and not primaries:
        room.characters[0].primary = True

    # A name must point at one character only.
    owners: dict[str, str] = {}
    for profile in room.characters:
        kept = []
        for alias in profile.aliases:
            key = alias.lower()
            if key in owners or key in (
                c.name.lower() for c in room.characters if c is not profile
            ):
                room.problems.append(
                    f"alias '{alias}' of {profile.id} is ambiguous; ignored"
                )
                continue
            owners[key] = profile.id
            kept.append(alias)
        profile.aliases = tuple(kept)

    if room.enabled:
        logger.info(
            f"VR Room loaded: {[f'{c.name} ({c.model}, {c.voice.describe()})' for c in room.characters]}"
            + (f" problems={room.problems}" if room.problems else "")
        )
    return room
