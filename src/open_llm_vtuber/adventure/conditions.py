"""Contextual eligibility rules shared by events and dialogue.

A condition is a small mapping written in YAML, for example::

    when: {env: [forest], weather: [rain], mood: {luna.annoyance: ">=2"}}

Every key must hold for the rule to be eligible. Unknown keys are reported at
load time so a typo never silently makes a line or event always eligible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

LIST_KEYS = (
    "env",
    "not_env",
    "region",
    "not_region",
    "region_tag",
    "time",
    "weather",
    "not_weather",
    "objective",
    "flags",
    "flags_any",
    "flags_none",
    "discovered",
    "not_discovered",
    "recent",
    "not_recent",
    "last_game",
    "activity",
    "place",
    "not_place",
    "place_tag",
    "visible",
    "not_visible",
)
NUMBER_KEYS = (
    "min_region_minutes",
    "min_since_rest",
    "max_since_rest",
    "min_progress",
    "max_progress",
)
KNOWN_KEYS = frozenset(LIST_KEYS + NUMBER_KEYS + ("mood",))
_COMPARE = re.compile(r"^\s*(>=|<=|==|>|<)?\s*(-?\d+(?:\.\d+)?)\s*$")


@dataclass
class Context:
    """What the world looks like right now, as rules see it."""

    env: str = ""
    region: str = ""
    region_tags: frozenset[str] = frozenset()
    time: str = ""
    weather: str = ""
    objective: str = ""
    activity: str = ""
    flags: frozenset[str] = frozenset()
    discovered: frozenset[str] = frozenset()
    recent: tuple[str, ...] = ()
    mood: dict[str, float] = field(default_factory=dict)
    region_minutes: float = 0.0
    since_rest: float = 0.0
    progress: float = 0.0  # whole journey, 0..1
    last_game: str = ""
    place: str = ""
    place_tags: frozenset[str] = frozenset()
    visible: frozenset[str] = frozenset()  # object TYPES visible right now


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(v) for v in value]
    return [str(value)]


def validate(when: Any, where: str) -> list[str]:
    """Problems with a condition mapping (empty list when it is fine)."""
    if when is None:
        return []
    if not isinstance(when, dict):
        return [f"{where}: 'when' must be a mapping"]
    problems = []
    for key, value in when.items():
        if key not in KNOWN_KEYS:
            problems.append(f"{where}: unknown condition '{key}'")
        elif key == "mood":
            if not isinstance(value, dict):
                problems.append(f"{where}: mood must be a mapping")
                continue
            for mood_key, expr in value.items():
                if "." not in str(mood_key) or not _COMPARE.match(str(expr)):
                    problems.append(f"{where}: bad mood rule {mood_key}: {expr}")
        elif key in NUMBER_KEYS:
            try:
                float(value)
            except (TypeError, ValueError):
                problems.append(f"{where}: {key} must be a number")
    return problems


def _compare(actual: float, expr: Any) -> bool:
    match = _COMPARE.match(str(expr))
    if not match:
        return False
    op, number = match.group(1) or ">=", float(match.group(2))
    return {
        ">=": actual >= number,
        "<=": actual <= number,
        "==": actual == number,
        ">": actual > number,
        "<": actual < number,
    }[op]


def _any(values: Iterable[str], pool: Iterable[str]) -> bool:
    pool = set(pool)
    return any(v in pool for v in values)


def matches(when: Any, ctx: Context) -> bool:
    """True when every rule in ``when`` holds for ``ctx``."""
    if not when:
        return True
    if not isinstance(when, dict):
        return False
    for key, value in when.items():
        items = _as_list(value)
        if key == "env" and ctx.env not in items:
            return False
        if key == "not_env" and ctx.env in items:
            return False
        if key == "region" and ctx.region not in items:
            return False
        if key == "not_region" and ctx.region in items:
            return False
        if key == "region_tag" and not _any(items, ctx.region_tags):
            return False
        if key == "time" and ctx.time not in items:
            return False
        if key == "weather" and ctx.weather not in items:
            return False
        if key == "not_weather" and ctx.weather in items:
            return False
        if key == "objective" and ctx.objective not in items:
            return False
        if key == "activity" and ctx.activity not in items:
            return False
        if key == "flags" and not all(v in ctx.flags for v in items):
            return False
        if key == "flags_any" and not _any(items, ctx.flags):
            return False
        if key == "flags_none" and _any(items, ctx.flags):
            return False
        if key == "discovered" and not all(v in ctx.discovered for v in items):
            return False
        if key == "not_discovered" and _any(items, ctx.discovered):
            return False
        if key == "recent" and not _any(items, ctx.recent):
            return False
        if key == "not_recent" and _any(items, ctx.recent):
            return False
        if key == "last_game" and ctx.last_game not in items:
            return False
        if key == "place" and ctx.place not in items:
            return False
        if key == "not_place" and ctx.place in items:
            return False
        if key == "place_tag" and not _any(items, ctx.place_tags):
            return False
        if key == "visible" and not all(v in ctx.visible for v in items):
            return False
        if key == "not_visible" and _any(items, ctx.visible):
            return False
        if key == "mood":
            for mood_key, expr in (value or {}).items():
                if not _compare(ctx.mood.get(str(mood_key), 0.0), expr):
                    return False
        if key == "min_region_minutes" and ctx.region_minutes < float(value):
            return False
        if key == "min_since_rest" and ctx.since_rest < float(value):
            return False
        if key == "max_since_rest" and ctx.since_rest > float(value):
            return False
        if key == "min_progress" and ctx.progress < float(value):
            return False
        if key == "max_progress" and ctx.progress > float(value):
            return False
    return True


def specificity(when: Any) -> int:
    """How many rules a condition has: more specific lines win ties."""
    return len(when) if isinstance(when, dict) else 0
