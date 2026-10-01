"""Adventure content: adventures, regions, objectives, places, events, dialogue.

Plain YAML under ``room/adventures``::

    room/adventures/<adventure>.yaml         one file per adventure
    room/adventures/library/events/*.yaml    shared, reusable events
    room/adventures/library/dialogue/*.yaml  shared, tagged dialogue exchanges

A journey is WORLD -> ADVENTURE -> REGION -> OBJECTIVE -> PLACE -> EVENT ->
BEAT. Progress is not a distance walked (the characters cannot walk): an
objective passes through real places (the rooftop, the old station road),
the characters stay a while at each (things happen), and moving on is a
traversal of the world around them. Nothing here is specific to one
adventure; a new journey is a new YAML file reusing everything else.
Loading never raises: broken entries are skipped and listed in
``library.problems``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml
from loguru import logger

from ..room.world_catalog import OBJECT_TYPES, ZONE_ORDER
from . import conditions

ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")

ENVIRONMENTS = ("city", "outskirts", "forest", "deep_forest", "river", "mountain", "valley", "ruins", "camp")
TIMES = ("dawn", "day", "dusk", "night")
WEATHERS = ("clear", "wind", "drizzle", "rain", "storm", "fog", "fireflies", "snow")
TRANSITIONS = ("travel", "magic", "fade")
CATEGORIES = ("ambient", "banter", "discovery", "obstacle", "setback", "weather", "rest", "activity", "milestone")
LOOK_TARGETS = ("LEFT", "RIGHT", "UP", "DOWN", "VIEWER", "NEUTRAL", "EACH_OTHER")
GESTURES = ("hop", "jump", "dance", "stumble")
SFX = (
    "owl", "bird", "rustle", "twig", "thunder", "howl", "chime", "magic", "fizzle", "splash",
    "rocks", "wind_gust", "discover", "footsteps", "creak", "crackle", "bell", "frog", "whoosh",
)
AMBIENCE = ("city_night", "forest_night", "forest_day", "river", "wind", "rain", "ruins", "campfire")
FORMATIONS = ("home", "gather", "apart")
BEAT_KINDS = (
    "sfx", "look", "react", "action", "say", "gesture", "glide", "formation", "spawn", "remove",
    "object_state", "magic", "weather", "camera", "set", "hold", "wait", "fx",
)
BEAT_MODIFIERS = ("wait", "chance")
FX = ("sparkles", "lightning", "poof")


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------


@dataclass
class EventDef:
    id: str
    category: str
    beats: list[dict[str, Any]]
    when: dict[str, Any] = field(default_factory=dict)
    weight: float = 1.0
    cooldown: float = 30.0  # active adventure minutes before it may run again
    max_per_place: int = 1
    once: bool = False  # at most once per adventure run
    resumable: bool = True  # after an interruption, finish the remaining beats
    activity: str = ""  # what the characters are doing while it runs
    note: str = ""  # causal history line when it starts ("a strange howl echoed")


@dataclass
class PropSpec:
    kind: str
    zone: str
    id: str = ""
    state: str = ""


@dataclass
class Place:
    id: str
    name: str  # "the old station road"
    stay: tuple[float, float] = (10.0, 16.0)  # active minutes spent here
    env: str = ""  # overrides the region
    time: str = ""
    activity: str = ""  # "looking for the trail markers"
    zones: dict[str, float] = field(default_factory=dict)
    zone_labels: dict[str, str] = field(default_factory=dict)
    props: list[PropSpec] = field(default_factory=list)
    arrive: str = ""  # optional event when they arrive
    tags: tuple[str, ...] = ()


@dataclass
class Objective:
    id: str
    text: str
    places: list[Place]
    finish_event: Optional[str] = None  # runs before the objective counts as done


@dataclass
class Region:
    id: str
    name: str
    env: str
    time: str
    description: str
    objectives: list[Objective]
    weather: dict[str, float] = field(default_factory=lambda: {"clear": 1.0})
    ambience: list[str] = field(default_factory=list)
    tags: tuple[str, ...] = ()
    transition: str = "travel"
    rest_allowed: bool = True


@dataclass
class Adventure:
    id: str
    title: str
    destination: str
    summary: str
    regions: list[Region]
    start_event: Optional[str] = None
    arrive_event: Optional[str] = None

    def places(self) -> list[tuple[int, int, int]]:
        """Every (region, objective, place) index in journey order."""
        return [
            (ri, oi, pi)
            for ri, r in enumerate(self.regions)
            for oi, o in enumerate(r.objectives)
            for pi, _ in enumerate(o.places)
        ]


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
    cooldown: float = 120.0  # active adventure minutes before it may play again
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

    def describe(self) -> dict[str, Any]:
        return {
            "adventures": list(self.adventures),
            "events": len(self.events),
            "exchanges": len(self.exchanges),
            "lines": sum(len(x.lines) for x in self.exchanges.values()),
            "triggers": len(self.by_trigger),
            "problems": list(self.problems[:20]),
        }


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def _num(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _who_ok(who: Any, cast: set[str]) -> bool:
    return who in ("all", "random", "other", "magic") or (who in cast if cast else True)


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
            if not isinstance(value, dict) or not all("." in str(k) for k in value):
                problems.append(f"{where}: bad mood effect")
        elif key == "discover":
            if not (isinstance(value, str) and ID_RE.match(value)):
                problems.append(f"{where}: bad discover id")
        elif key == "note":
            if not isinstance(value, str):
                problems.append(f"{where}: note must be text")
        elif key == "delay":
            try:
                if not 0 <= float(value) <= 60:
                    raise ValueError
            except (TypeError, ValueError):
                problems.append(f"{where}: delay must be 0 to 60 minutes")
        else:
            problems.append(f"{where}: unknown effect '{key}'")
    return problems


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
    kind, value = kinds[0], beat[kinds[0]]
    p: list[str] = []

    def obj_ok(v: Any) -> bool:
        return isinstance(v, str) and bool(ID_RE.match(v))

    if kind == "sfx":
        name = value.get("name") if isinstance(value, dict) else value
        if name not in SFX:
            p.append(f"{where}: unknown sfx '{name}'")
    elif kind == "look":
        if not isinstance(value, dict):
            p.append(f"{where}: look needs a mapping")
        elif value.get("at") not in LOOK_TARGETS and not str(value.get("at", "")).startswith("object:"):
            p.append(f"{where}: look at must be one of {LOOK_TARGETS} or object:<id>")
        elif not _who_ok(value.get("who", "all"), cast):
            p.append(f"{where}: unknown who '{value.get('who')}'")
    elif kind == "react":
        if not isinstance(value, dict) or not isinstance(value.get("reaction"), str):
            p.append(f"{where}: react needs a reaction")
        elif not _who_ok(value.get("who", "random"), cast):
            p.append(f"{where}: unknown who '{value.get('who')}'")
    elif kind == "action":
        if not isinstance(value, dict) or not isinstance(value.get("name"), str) or not _who_ok(value.get("who"), cast):
            p.append(f"{where}: action needs who and name")
    elif kind == "say":
        if not isinstance(value, dict) or not isinstance(value.get("trigger"), str):
            p.append(f"{where}: say needs a trigger")
    elif kind == "gesture":
        if not isinstance(value, dict) or value.get("type") not in GESTURES:
            p.append(f"{where}: gesture type must be one of {GESTURES}")
    elif kind == "glide":
        if not isinstance(value, dict) or not isinstance(value.get("zone"), str):
            p.append(f"{where}: glide needs who and zone")
    elif kind == "formation" and value not in FORMATIONS:
        p.append(f"{where}: formation must be one of {FORMATIONS}")
    elif kind == "spawn":
        if not isinstance(value, dict) or value.get("kind") not in OBJECT_TYPES or not obj_ok(value.get("id", "")):
            p.append(f"{where}: spawn needs an id and a kind the renderer knows")
    elif kind in ("remove", "object_state"):
        if not isinstance(value, dict) or not obj_ok(value.get("id", "")):
            p.append(f"{where}: {kind} needs an object id")
    elif kind == "magic":
        if not isinstance(value, dict) or not obj_ok(value.get("target", "")):
            p.append(f"{where}: magic needs a target object id")
    elif kind == "weather" and value not in WEATHERS:
        p.append(f"{where}: unknown weather '{value}'")
    elif kind == "fx":
        if not isinstance(value, dict) or value.get("name") not in FX:
            p.append(f"{where}: fx name must be one of {FX}")
    elif kind == "camera":
        if not isinstance(value, dict) or value.get("shot") not in ("wide", "two_shot", "focus", "closeup"):
            p.append(f"{where}: camera shot must be wide, two_shot, focus or closeup")
    elif kind == "set":
        p += validate_effects(value, where)
    elif kind in ("hold", "wait"):
        try:
            if not 0 <= float(value) <= 900:
                raise ValueError
        except (TypeError, ValueError):
            p.append(f"{where}: {kind} must be 0 to 900 seconds")
    if "chance" in beat:
        try:
            float(beat["chance"])
        except (TypeError, ValueError):
            p.append(f"{where}: chance must be a number")
    return p


# ---------------------------------------------------------------------------
# parsing
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
            max_per_place=int(_num(raw.get("max_per_place"), 0, 100, 1)),
            once=bool(raw.get("once", False)),
            resumable=bool(raw.get("resumable", True)),
            activity=str(raw.get("activity") or "")[:80],
            note=str(raw.get("note") or "")[:120],
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
    if not re.match(r"^[a-z][a-z0-9_]{0,60}$", trigger):
        problems.append(f"{where}: bad trigger '{trigger}'")
    problems += conditions.validate(raw.get("when"), where)
    problems += validate_effects(raw.get("effects"), where)
    lines = []
    for i, item in enumerate(raw.get("lines") or []):
        if isinstance(item, (list, tuple)) and 2 <= len(item) <= 3:
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
            cooldown=_num(raw.get("cooldown"), 0, 100000, 120),
            effects=raw.get("effects") or {},
        ),
        [],
    )


def _parse_place(raw: Any, where: str, events: dict[str, EventDef], problems: list[str]) -> Optional[Place]:
    if not isinstance(raw, dict) or not ID_RE.match(str(raw.get("id", ""))):
        problems.append(f"{where}: a place needs an id")
        return None
    pid = raw["id"]
    env = str(raw.get("env") or "")
    if env and env not in ENVIRONMENTS:
        problems.append(f"{where} place {pid}: unknown env '{env}'")
        env = ""
    time_ = str(raw.get("time") or "")
    if time_ and time_ not in TIMES:
        problems.append(f"{where} place {pid}: unknown time '{time_}'")
        time_ = ""
    stay = raw.get("stay") or [10, 16]
    try:
        lo, hi = float(stay[0]), float(stay[1])
        stay_t = (max(0.5, lo), max(lo, hi))
    except (TypeError, ValueError, IndexError):
        problems.append(f"{where} place {pid}: stay must be [min, max] minutes")
        stay_t = (10.0, 16.0)
    zones, labels = {}, {}
    for name, spec in (raw.get("zones") or {}).items():
        if not ID_RE.match(str(name)) or name in ZONE_ORDER:
            problems.append(f"{where} place {pid}: bad zone name '{name}'")
            continue
        if isinstance(spec, dict):
            zones[name] = _num(spec.get("x"), 0.05, 0.95, 0.5)
            labels[name] = str(spec.get("label") or f"the {name.replace('_', ' ')}")[:40]
        else:
            zones[name] = _num(spec, 0.05, 0.95, 0.5)
            labels[name] = f"the {name.replace('_', ' ')}"
    props = []
    for prop in raw.get("props") or []:
        if not isinstance(prop, dict) or prop.get("kind") not in OBJECT_TYPES:
            problems.append(f"{where} place {pid}: prop kind unknown {prop}")
            continue
        zone = str(prop.get("zone") or "center")
        if zone not in ZONE_ORDER and zone not in zones:
            problems.append(f"{where} place {pid}: prop zone '{zone}' does not exist")
            continue
        prop_id = str(prop.get("id") or "")
        if prop_id and not ID_RE.match(prop_id):
            problems.append(f"{where} place {pid}: bad prop id")
            continue
        props.append(PropSpec(prop["kind"], zone, prop_id, str(prop.get("state") or "")))
    arrive = str(raw.get("arrive") or "")
    if arrive and arrive not in events:
        problems.append(f"{where} place {pid}: arrive event '{arrive}' unknown")
        arrive = ""
    return Place(
        id=pid,
        name=str(raw.get("name") or pid.replace("_", " "))[:60],
        stay=stay_t,
        env=env,
        time=time_,
        activity=str(raw.get("activity") or "")[:80],
        zones=zones,
        zone_labels=labels,
        props=props,
        arrive=arrive,
        tags=tuple(str(t) for t in (raw.get("tags") or [])),
    )


def parse_adventure(raw: Any, events: dict[str, EventDef], where: str) -> tuple[Optional[Adventure], list[str]]:
    if not isinstance(raw, dict):
        return None, [f"{where}: must be a mapping"]
    adv_id = str(raw.get("id", ""))
    where = f"adventure {adv_id or where}"
    problems: list[str] = []
    if not ID_RE.match(adv_id):
        return None, [f"{where}: bad id"]

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
        env, time_ = r.get("env"), r.get("time", "night")
        if env not in ENVIRONMENTS or time_ not in TIMES:
            problems.append(f"{where}: region {rid} has a bad env or time")
            continue
        weather = r.get("weather") or {"clear": 1}
        if not isinstance(weather, dict) or any(w not in WEATHERS for w in weather):
            problems.append(f"{where}: region {rid} has unknown weather {weather}")
            weather = {"clear": 1}
        ambience = [a for a in _list(r.get("ambience")) if a in AMBIENCE]
        objectives = []
        for o in r.get("objectives") or []:
            if not isinstance(o, dict) or not ID_RE.match(str(o.get("id", ""))):
                problems.append(f"{where}: region {rid} has a bad objective")
                continue
            places = [
                p
                for p in (
                    _parse_place(x, f"{where} objective {o['id']}", events, problems)
                    for x in (o.get("places") or [])
                )
                if p
            ]
            if not places:
                problems.append(f"{where}: objective {o['id']} has no places")
                continue
            objectives.append(
                Objective(
                    id=o["id"],
                    text=str(o.get("text") or o["id"])[:120],
                    places=places,
                    finish_event=event_ref(o.get("finish_event"), f"objective {o['id']}"),
                )
            )
        if not objectives:
            problems.append(f"{where}: region {rid} has no objectives")
            continue
        transition = str(r.get("transition") or "travel")
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
                transition=transition if transition in TRANSITIONS else "travel",
                rest_allowed=bool(r.get("rest_allowed", True)),
            )
        )
    if not regions:
        return None, problems + [f"{where}: no usable regions"]
    return (
        Adventure(
            id=adv_id,
            title=str(raw.get("title") or adv_id)[:80],
            destination=str(raw.get("destination") or "")[:80],
            summary=str(raw.get("summary") or "")[:300],
            regions=regions,
            start_event=event_ref(raw.get("start_event"), "start_event"),
            arrive_event=event_ref(raw.get("arrive_event"), "arrive_event"),
        ),
        problems,
    )


def _yaml_files(folder: Path) -> Iterable[Path]:
    return sorted(folder.glob("*.yaml")) if folder.is_dir() else []


def load_library(folder: Path | str, cast: Iterable[str] = ()) -> AdventureLibrary:
    """Load every adventure, event and dialogue file. Never raises."""
    folder = Path(folder)
    cast_ids = set(cast)
    lib = AdventureLibrary()
    for path in _yaml_files(folder / "library" / "events"):
        try:
            for event_id, raw in ((_read(path) or {}).get("events") or {}).items():
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
            for i, raw in enumerate((_read(path) or {}).get("exchanges") or []):
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
    for event in lib.events.values():
        for beat in event.beats:
            say = beat.get("say")
            if isinstance(say, dict) and say.get("trigger") not in lib.by_trigger:
                lib.problems.append(f"event {event.id}: say trigger '{say.get('trigger')}' has no lines")
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
