"""Adventure content: adventures, regions, objectives, events and dialogue.

Content is plain YAML under ``room/adventures``::

    room/adventures/<adventure>.yaml        one file per adventure
    room/adventures/library/events/*.yaml   shared, reusable event library
    room/adventures/library/dialogue/*.yaml shared, tagged dialogue exchanges

Nothing here is specific to one adventure. A new journey is a new YAML file
that reuses the environments, events and dialogue pools that already exist.
Loading never raises: broken entries are skipped and reported in
``library.problems`` so one typo cannot take the stream down.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml
from loguru import logger

from . import conditions

ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")

# Environment kits the scene composer (frontend/vr-agent/room-world.js) can draw.
ENVIRONMENTS = (
    "city",
    "outskirts",
    "forest",
    "deep_forest",
    "river",
    "mountain",
    "valley",
    "ruins",
    "camp",
)
TIMES = ("dawn", "day", "dusk", "night")
WEATHERS = ("clear", "wind", "drizzle", "rain", "storm", "fog", "fireflies", "snow")
CATEGORIES = (
    "ambient",
    "banter",
    "discovery",
    "obstacle",
    "setback",
    "weather",
    "rest",
    "challenge",
    "milestone",
)
LOOK_TARGETS = (
    "LEFT",
    "RIGHT",
    "UP",
    "DOWN",
    "VIEWER",
    "NEUTRAL",
    "EACH_OTHER",
)
MOVES = ("hop", "jump", "step_forward", "step_back", "dance", "spin", "stumble")
BEAT_KINDS = (
    "sfx",
    "look",
    "react",
    "action",
    "say",
    "move",
    "travel",
    "effect",
    "weather",
    "prop",
    "prop_remove",
    "camera",
    "set",
    "hold",
    "wait",
    "formation",
)
FORMATIONS = ("travel", "gather", "apart", "rest", "lead_mika", "lead_luna")
BEAT_MODIFIERS = ("wait", "chance", "who", "pan")
EFFECTS = (
    "lightning",
    "sparkles",
    "magic_burst",
    "magic_fizzle",
    "smoke_puff",
    "leaves_gust",
    "fog_pulse",
    "dust_fall",
    "rune_glow",
    "shimmer",
    "stars_fall",
    "splash",
)
PROPS = (
    "signpost",
    "lantern",
    "campfire",
    "glow_stone",
    "rabbit",
    "fallen_log",
    "rope_bridge",
    "crystal",
    "shrine",
    "boulder",
    "map_scroll",
    "mushrooms",
)
SFX = (
    "owl",
    "bird",
    "rustle",
    "twig",
    "thunder",
    "howl",
    "chime",
    "magic",
    "fizzle",
    "splash",
    "rocks",
    "wind_gust",
    "discover",
    "footsteps",
    "creak",
    "crackle",
    "bell",
    "frog",
)
AMBIENCE = (
    "city_night",
    "forest_night",
    "forest_day",
    "river",
    "wind",
    "rain",
    "ruins",
    "campfire",
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class EventDef:
    id: str
    category: str
    beats: list[dict[str, Any]]
    when: dict[str, Any] = field(default_factory=dict)
    weight: float = 1.0
    cooldown: float = 30.0  # active adventure minutes
    max_per_region: int = 2
    once: bool = False  # at most once per adventure run
    resumable: bool = True  # after an interruption, finish the remaining beats
    activity: str = ""  # what the characters are doing, for the viewer context
    note: str = ""  # one short line for the viewer context ("a strange howl")


@dataclass
class Objective:
    id: str
    text: str
    distance: float
    finish_event: Optional[str] = None
    activity: str = "walking"
    events: tuple[str, ...] = ()  # events favoured while this objective is active


@dataclass
class Region:
    id: str
    name: str
    env: str
    time: str
    description: str
    objectives: list[Objective]
    weather: dict[str, float] = field(default_factory=lambda: {"clear": 1.0})
    ambience: str = ""
    tags: tuple[str, ...] = ()
    rest_allowed: bool = True
    enter_event: Optional[str] = None


@dataclass
class Adventure:
    id: str
    title: str
    destination: str
    summary: str
    regions: list[Region]
    travel_speed: float = 1.0  # distance units per active travel minute
    start_event: Optional[str] = None
    arrive_event: Optional[str] = None

    @property
    def total_distance(self) -> float:
        return sum(o.distance for r in self.regions for o in r.objectives) or 1.0

    def region(self, index: int) -> Region:
        return self.regions[max(0, min(index, len(self.regions) - 1))]


@dataclass
class Line:
    who: str
    text: str
    reaction: Optional[str] = None


@dataclass
class Exchange:
    id: str
    trigger: str
    lines: list[Line]
    when: dict[str, Any] = field(default_factory=dict)
    weight: float = 1.0
    cooldown: float = 90.0  # active adventure minutes before it may play again
    effects: dict[str, Any] = field(default_factory=dict)

    def line_id(self, index: int) -> str:
        return f"{self.id}.{index}"


@dataclass
class AdventureLibrary:
    adventures: dict[str, Adventure] = field(default_factory=dict)
    events: dict[str, EventDef] = field(default_factory=dict)
    exchanges: dict[str, Exchange] = field(default_factory=dict)
    by_trigger: dict[str, list[Exchange]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def triggers(self) -> list[str]:
        return sorted(self.by_trigger)

    def describe(self) -> dict[str, Any]:
        return {
            "adventures": list(self.adventures),
            "events": len(self.events),
            "exchanges": len(self.exchanges),
            "lines": sum(len(x.lines) for x in self.exchanges.values()),
            "problems": list(self.problems[:20]),
        }


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _num(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def _who_ok(who: Any, cast: set[str]) -> bool:
    return who in ("all", "random", "other") or (who in cast if cast else True)


def validate_effects(effects: Any, where: str) -> list[str]:
    if not effects:
        return []
    if not isinstance(effects, dict):
        return [f"{where}: effects must be a mapping"]
    problems = []
    for key, value in effects.items():
        if key in ("flags", "unset"):
            if not all(isinstance(v, str) and ID_RE.match(v) for v in _list(value)):
                problems.append(f"{where}: bad {key}")
        elif key == "mood":
            if not isinstance(value, dict) or not all(
                "." in str(k) for k in value
            ):
                problems.append(f"{where}: bad mood effect")
        elif key == "discover":
            if not (isinstance(value, str) and ID_RE.match(value)):
                problems.append(f"{where}: bad discover id")
        elif key == "progress":
            try:
                float(value)
            except (TypeError, ValueError):
                problems.append(f"{where}: progress must be a number")
        else:
            problems.append(f"{where}: unknown effect '{key}'")
    return problems


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def validate_beat(beat: Any, where: str, cast: set[str]) -> list[str]:
    if not isinstance(beat, dict):
        return [f"{where}: beat must be a mapping"]
    kinds = [k for k in beat if k in BEAT_KINDS and k != "wait"]
    if not kinds and "wait" in beat:
        kinds = ["wait"]
    unknown = [k for k in beat if k not in BEAT_KINDS and k not in BEAT_MODIFIERS]
    if unknown:
        return [f"{where}: unknown beat keys {unknown}"]
    if len(kinds) != 1:
        return [f"{where}: a beat needs exactly one kind, got {kinds}"]
    kind = kinds[0]
    value = beat[kind]
    p: list[str] = []
    if "chance" in beat:
        try:
            float(beat["chance"])
        except (TypeError, ValueError):
            p.append(f"{where}: chance must be a number")
    if kind == "sfx" and value not in SFX:
        p.append(f"{where}: unknown sfx '{value}'")
    elif kind == "look":
        if not isinstance(value, dict) or value.get("at") not in LOOK_TARGETS:
            p.append(f"{where}: look needs at in {LOOK_TARGETS}")
        elif not _who_ok(value.get("who", "all"), cast):
            p.append(f"{where}: unknown who '{value.get('who')}'")
    elif kind == "react":
        if not isinstance(value, dict) or not isinstance(value.get("reaction"), str):
            p.append(f"{where}: react needs a reaction")
        elif not _who_ok(value.get("who", "random"), cast):
            p.append(f"{where}: unknown who '{value.get('who')}'")
    elif kind == "action":
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            p.append(f"{where}: action needs a name")
        elif value.get("who") not in cast and cast:
            p.append(f"{where}: action needs a real character")
    elif kind == "say":
        if not isinstance(value, dict) or not isinstance(value.get("trigger"), str):
            p.append(f"{where}: say needs a trigger")
    elif kind == "move":
        if not isinstance(value, dict) or value.get("type") not in MOVES:
            p.append(f"{where}: move type must be one of {MOVES}")
        elif not _who_ok(value.get("who", "random"), cast):
            p.append(f"{where}: unknown who '{value.get('who')}'")
    elif kind == "travel" and not isinstance(value, bool):
        p.append(f"{where}: travel must be true or false")
    elif kind == "effect":
        name = value.get("name") if isinstance(value, dict) else value
        if name not in EFFECTS:
            p.append(f"{where}: unknown effect '{name}'")
    elif kind == "weather" and value not in WEATHERS:
        p.append(f"{where}: unknown weather '{value}'")
    elif kind == "prop":
        if (
            not isinstance(value, dict)
            or value.get("kind") not in PROPS
            or not ID_RE.match(str(value.get("id", "")))
        ):
            p.append(f"{where}: prop needs an id and a kind in {PROPS}")
    elif kind == "prop_remove" and not ID_RE.match(str(value)):
        p.append(f"{where}: prop_remove needs a prop id")
    elif kind == "camera":
        if not isinstance(value, dict) or value.get("shot") not in (
            "wide",
            "two_shot",
            "focus",
            "closeup",
        ):
            p.append(f"{where}: camera shot must be wide, two_shot, focus or closeup")
    elif kind == "set":
        p += validate_effects(value, where)
    elif kind == "formation" and value not in FORMATIONS:
        p.append(f"{where}: formation must be one of {FORMATIONS}")
    elif kind in ("hold", "wait"):
        try:
            if not 0 <= float(value) <= 900:
                raise ValueError
        except (TypeError, ValueError):
            p.append(f"{where}: {kind} must be 0 to 900 seconds")
    return p


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _read(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def parse_event(event_id: str, raw: Any, cast: set[str]) -> tuple[Optional[EventDef], list[str]]:
    where = f"event {event_id}"
    if not ID_RE.match(str(event_id)) or not isinstance(raw, dict):
        return None, [f"{where}: bad id or not a mapping"]
    problems = conditions.validate(raw.get("when"), where)
    category = raw.get("category", "ambient")
    if category not in CATEGORIES:
        problems.append(f"{where}: unknown category '{category}'")
    beats = raw.get("beats") or []
    if not isinstance(beats, list) or not beats:
        problems.append(f"{where}: needs a list of beats")
        beats = []
    for i, beat in enumerate(beats):
        problems += validate_beat(beat, f"{where} beat {i}", cast)
    if problems:
        return None, problems
    return (
        EventDef(
            id=event_id,
            category=category,
            beats=beats,
            when=raw.get("when") or {},
            weight=_num(raw.get("weight"), 0, 100, 1),
            cooldown=_num(raw.get("cooldown"), 0, 100000, 30),
            max_per_region=int(_num(raw.get("max_per_region"), 0, 100, 2)),
            once=bool(raw.get("once", False)),
            resumable=bool(raw.get("resumable", True)),
            activity=str(raw.get("activity") or "")[:80],
            note=str(raw.get("note") or "")[:100],
        ),
        [],
    )


def parse_exchange(raw: Any, cast: set[str], where: str) -> tuple[Optional[Exchange], list[str]]:
    if not isinstance(raw, dict):
        return None, [f"{where}: exchange must be a mapping"]
    ex_id = str(raw.get("id", ""))
    where = f"dialogue {ex_id or where}"
    problems = []
    if not ID_RE.match(ex_id):
        problems.append(f"{where}: bad id")
    trigger = str(raw.get("trigger", ""))
    if not re.match(r"^[a-z][a-z0-9_:]{0,60}$", trigger):
        problems.append(f"{where}: bad trigger '{trigger}'")
    problems += conditions.validate(raw.get("when"), where)
    problems += validate_effects(raw.get("effects"), where)
    lines = []
    for i, item in enumerate(raw.get("lines") or []):
        if isinstance(item, dict):
            who, text, reaction = item.get("who"), item.get("say"), item.get("do")
        elif isinstance(item, (list, tuple)) and 2 <= len(item) <= 3:
            who, text = item[0], item[1]
            reaction = item[2] if len(item) == 3 else None
        else:
            problems.append(f"{where}: line {i} must be [who, text] or [who, text, reaction]")
            continue
        text = " ".join(str(text or "").split())
        if (cast and who not in cast) or not text or len(text) > 180:
            problems.append(f"{where}: line {i} has an unknown speaker or bad text")
            continue
        lines.append(Line(str(who), text, str(reaction) if reaction else None))
    if not lines:
        problems.append(f"{where}: no lines")
    if problems:
        return None, problems
    return (
        Exchange(
            id=ex_id,
            trigger=trigger,
            lines=lines,
            when=raw.get("when") or {},
            weight=_num(raw.get("weight"), 0, 100, 1),
            cooldown=_num(raw.get("cooldown"), 0, 100000, 90),
            effects=raw.get("effects") or {},
        ),
        [],
    )


def parse_adventure(raw: Any, events: dict[str, EventDef], where: str) -> tuple[Optional[Adventure], list[str]]:
    if not isinstance(raw, dict):
        return None, [f"{where}: must be a mapping"]
    adv_id = str(raw.get("id", ""))
    where = f"adventure {adv_id or where}"
    problems = []
    if not ID_RE.match(adv_id):
        problems.append(f"{where}: bad id")

    def event_ref(name: Any, ctx: str) -> Optional[str]:
        if name is None:
            return None
        if name not in events:
            problems.append(f"{where}: {ctx} refers to unknown event '{name}'")
            return None
        return str(name)

    regions = []
    for r_i, r in enumerate(raw.get("regions") or []):
        if not isinstance(r, dict) or not ID_RE.match(str(r.get("id", ""))):
            problems.append(f"{where}: region {r_i} needs an id")
            continue
        rid = r["id"]
        scene = r.get("scene") or {}
        env = scene.get("env")
        time_ = scene.get("time", "night")
        if env not in ENVIRONMENTS:
            problems.append(f"{where}: region {rid} has unknown env '{env}'")
        if time_ not in TIMES:
            problems.append(f"{where}: region {rid} has unknown time '{time_}'")
        weather = r.get("weather") or {"clear": 1}
        if not isinstance(weather, dict) or any(w not in WEATHERS for w in weather):
            problems.append(f"{where}: region {rid} has unknown weather {weather}")
            weather = {"clear": 1}
        ambience = str(r.get("ambience") or "")
        if ambience and ambience not in AMBIENCE:
            problems.append(f"{where}: region {rid} has unknown ambience '{ambience}'")
        objectives = []
        for o in r.get("objectives") or []:
            if not isinstance(o, dict) or not ID_RE.match(str(o.get("id", ""))):
                problems.append(f"{where}: region {rid} has a bad objective")
                continue
            favoured = tuple(
                e for e in (o.get("events") or []) if event_ref(e, f"objective {o['id']}")
            )
            objectives.append(
                Objective(
                    id=o["id"],
                    text=str(o.get("text") or o["id"])[:120],
                    distance=_num(o.get("distance"), 0.5, 1000, 10),
                    finish_event=event_ref(o.get("finish_event"), f"objective {o['id']}"),
                    activity=str(o.get("activity") or "walking")[:80],
                    events=favoured,
                )
            )
        if not objectives:
            problems.append(f"{where}: region {rid} has no objectives")
            continue
        regions.append(
            Region(
                id=rid,
                name=str(r.get("name") or rid)[:40],
                env=str(env),
                time=str(time_),
                description=str(r.get("description") or "")[:240],
                objectives=objectives,
                weather={str(k): _num(v, 0, 100, 1) for k, v in weather.items()},
                ambience=ambience,
                tags=tuple(str(t) for t in (r.get("tags") or [])),
                rest_allowed=bool(r.get("rest_allowed", True)),
                enter_event=event_ref(r.get("enter_event"), f"region {rid}"),
            )
        )
    if not regions:
        problems.append(f"{where}: no usable regions")
    adventure = Adventure(
        id=adv_id,
        title=str(raw.get("title") or adv_id)[:80],
        destination=str(raw.get("destination") or "")[:80],
        summary=str(raw.get("summary") or "")[:300],
        regions=regions,
        travel_speed=_num(raw.get("travel_speed"), 0.05, 20, 1),
        start_event=event_ref(raw.get("start_event"), "start_event"),
        arrive_event=event_ref(raw.get("arrive_event"), "arrive_event"),
    )
    fatal = [p for p in problems if "bad id" in p or "no usable regions" in p]
    return (None if fatal else adventure), problems


def _yaml_files(folder: Path) -> Iterable[Path]:
    return sorted(folder.glob("*.yaml")) if folder.is_dir() else []


def load_library(folder: Path | str, cast: Iterable[str] = ()) -> AdventureLibrary:
    """Load every adventure, event and dialogue file. Never raises."""
    folder = Path(folder)
    cast_ids = set(cast)
    lib = AdventureLibrary()

    for path in _yaml_files(folder / "library" / "events"):
        try:
            data = _read(path) or {}
            for event_id, raw in (data.get("events") or {}).items():
                if event_id in lib.events:
                    lib.problems.append(f"event {event_id}: defined twice")
                    continue
                event, problems = parse_event(event_id, raw, cast_ids)
                lib.problems += problems
                if event:
                    lib.events[event_id] = event
        except Exception as exc:
            lib.problems.append(f"{path.name}: {exc}")

    for path in _yaml_files(folder / "library" / "dialogue"):
        try:
            data = _read(path) or {}
            for i, raw in enumerate(data.get("exchanges") or []):
                exchange, problems = parse_exchange(raw, cast_ids, f"{path.name}#{i}")
                lib.problems += problems
                if not exchange:
                    continue
                if exchange.id in lib.exchanges:
                    lib.problems.append(f"dialogue {exchange.id}: defined twice")
                    continue
                lib.exchanges[exchange.id] = exchange
                lib.by_trigger.setdefault(exchange.trigger, []).append(exchange)
        except Exception as exc:
            lib.problems.append(f"{path.name}: {exc}")

    # 'say' beats must point at a trigger that has lines.
    for event in lib.events.values():
        for beat in event.beats:
            say = beat.get("say")
            if isinstance(say, dict) and say.get("trigger") not in lib.by_trigger:
                lib.problems.append(
                    f"event {event.id}: say trigger '{say.get('trigger')}' has no lines"
                )

    for path in _yaml_files(folder):
        try:
            adventure, problems = parse_adventure(_read(path), lib.events, path.name)
            lib.problems += problems
            if adventure:
                lib.adventures[adventure.id] = adventure
        except Exception as exc:
            lib.problems.append(f"{path.name}: {exc}")

    if lib.problems:
        logger.warning(f"Adventure: {len(lib.problems)} content problem(s): {lib.problems[:5]}")
    return lib
