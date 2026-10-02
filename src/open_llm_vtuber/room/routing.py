"""Deterministic message routing: who should answer a viewer message.

No LLM call. Names, group words, requested actions, topics and fairness
decide the speaker. Ambiguous messages fall back to fairness, which is good
enough and costs nothing.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Optional

from ..vr_agent.intent import detect_intent
from .profiles import RoomConfig
from .state import RoomState

GROUP_RE = re.compile(
    r"\b(?:both(?: of you)?|you two|you guys|you girls|each of you|all of you|everyone|"
    r"y'?all|you both|two of you)\b",
    re.I,
)
COMPARE_RE = re.compile(
    r"\b(?:which (?:one )?of you|who of you|which one is|who(?:'s| is) (?:the )?(?:\w+er|more|most|best|better|worse|worst)\b|"
    r"who (?:would|will|can|could) win|between you)\b",
    re.I,
)
COMPLIMENT_RE = re.compile(
    r"\b(?:cute|pretty|beautiful|adorable|love (?:you|u)|best|cool|amazing|awesome|nice|smart|talented|queen|slay)\b",
    re.I,
)
TEASE_RE = re.compile(
    r"\b(?:dumb|stupid|boring|cringe|ugly|weird|sus|liar|lying|fake|bad at|loser|noob)\b",
    re.I,
)


@dataclass
class RoutingDecision:
    mode: str  # single | group | compare | pair
    speakers: list[str]  # in speaking order
    addressed: list[str] = field(default_factory=list)  # characters the viewer named
    reason: str = ""
    intent: Optional[str] = None
    compliment: bool = False
    tease: bool = False

    @property
    def first(self) -> Optional[str]:
        return self.speakers[0] if self.speakers else None


def _name_pattern(name: str) -> str:
    """A name regex that tolerates how viewers really type it.

    Chat stretches names ("Mikaaaa", "Luuuna", "mikka"), so every letter may
    repeat. The word guards around it stay, so "mikado" still never matches.
    """
    return "".join(
        (re.escape(ch) + "+") if ch.isalpha() else re.escape(ch) for ch in name
    )


def _mentions(
    text: str, room: RoomConfig, available: list[str]
) -> list[tuple[int, str]]:
    lowered = f" {text.lower()} "
    found: list[tuple[int, str]] = []
    for profile in room.characters:
        if profile.id not in available:
            continue
        best = None
        for name in profile.names:
            match = re.search(rf"(?<![a-z0-9]){_name_pattern(name)}(?![a-z0-9])", lowered)
            if match and (best is None or match.start() < best):
                best = match.start()
        if best is not None:
            found.append((best, profile.id))
    return sorted(found)


def _pair(text: str, room: RoomConfig, ids: list[str]) -> Optional[tuple[str, str]]:
    names = {name: p.id for p in room.characters if p.id in ids for name in p.names}
    alternation = "|".join(sorted((re.escape(n) for n in names), key=len, reverse=True))
    if not alternation:
        return None
    match = re.search(
        rf"(?P<a>{alternation})\s*,?\s*(?:can you |could you |please |pls )?(?:ask|tell|question|challenge|roast|tease|compliment)\s+(?P<b>{alternation})\b",
        text.lower(),
    )
    if not match:
        match = re.search(
            rf"\b(?:can|could|should|will)\s+(?P<a>{alternation})\s+(?:ask|tell|question|challenge|roast|tease|compliment)\s+(?P<b>{alternation})\b",
            text.lower(),
        )
    if match and names[match.group("a")] != names[match.group("b")]:
        return names[match.group("a")], names[match.group("b")]
    return None


def _topic_scores(text: str, room: RoomConfig, ids: list[str]) -> dict[str, int]:
    lowered = text.lower()
    scores: dict[str, int] = {}
    for profile in room.characters:
        if profile.id not in ids:
            continue
        scores[profile.id] = sum(
            1
            for topic in profile.topics
            if re.search(rf"(?<![a-z]){re.escape(topic)}", lowered)
        )
    return scores


def _fair_pick(
    candidates: list[str],
    room: RoomConfig,
    state: RoomState,
    rng: random.Random,
    window: int,
) -> str:
    recent = [line.speaker for line in list(state.recent_lines)[-window:]]

    def weight(cid: str) -> float:
        profile = room.get(cid)
        w = profile.talkativeness if profile else 1.0
        w /= 1.0 + recent.count(cid)
        if cid == state.current_speaker or cid == state.previous_speaker:
            w *= 0.6
        return w

    weights = [weight(c) for c in candidates]
    total = sum(weights)
    if total <= 0:
        return candidates[0]
    cursor = rng.random() * total
    for cid, w in zip(candidates, weights):
        if cursor < w:
            return cid
        cursor -= w
    return candidates[-1]


def route_message(
    text: str,
    room: RoomConfig,
    state: RoomState,
    rng: Optional[random.Random] = None,
) -> RoutingDecision:
    rng = rng or random.Random()
    available = state.available_characters()
    if not available:
        return RoutingDecision("single", [], reason="no character available")
    compliment = bool(COMPLIMENT_RE.search(text))
    tease = bool(TEASE_RE.search(text))
    base = dict(compliment=compliment, tease=tease)

    if len(available) >= 2:
        pair = _pair(text, room, available)
        if pair:
            return RoutingDecision(
                "pair", list(pair), addressed=list(pair), reason="pair request", **base
            )

    mentioned = [cid for _, cid in _mentions(text, room, available)]
    compare = bool(COMPARE_RE.search(text))
    group = bool(GROUP_RE.search(text))

    if len(mentioned) >= 2:
        mode = "compare" if compare else "group"
        return RoutingDecision(
            mode, mentioned[:2], addressed=mentioned, reason="several names", **base
        )

    intent = detect_intent(_strip_names(text, room))
    if len(mentioned) == 1:
        first = mentioned[0]
        others = [c for c in available if c != first]
        if (group or compare) and others:
            return RoutingDecision(
                "compare" if compare else "group",
                [first, others[0]],
                addressed=mentioned,
                reason="named plus group words",
                intent=intent,
                **base,
            )
        return RoutingDecision(
            "single",
            [first],
            addressed=mentioned,
            reason="named",
            intent=intent,
            **base,
        )

    if (group or compare) and len(available) >= 2:
        first = _fair_pick(available, room, state, rng, room.director.fairness_window)
        second = next(c for c in available if c != first)
        return RoutingDecision(
            "compare" if compare else "group",
            [first, second],
            reason="group words",
            intent=intent,
            **base,
        )

    if intent:
        able = [
            cid
            for cid in available
            if (p := room.get(cid))
            and p.capabilities
            and p.capabilities.action_for_intent(intent)
        ]
        if len(able) == 1:
            return RoutingDecision(
                "single",
                able,
                reason=f"only {able[0]} can {intent}",
                intent=intent,
                **base,
            )
        if able:
            available = able

    scores = _topic_scores(text, room, available)
    top = max(scores.values()) if scores else 0
    if top > 0:
        leaders = [cid for cid, s in scores.items() if s == top]
        if len(leaders) == 1:
            return RoutingDecision(
                "single", leaders, reason="topic", intent=intent, **base
            )
        available = leaders

    pick = _fair_pick(available, room, state, rng, room.director.fairness_window)
    return RoutingDecision("single", [pick], reason="fairness", intent=intent, **base)


def _strip_names(text: str, room: RoomConfig) -> str:
    out = text
    for profile in room.characters:
        for name in profile.names:
            out = re.sub(
                rf"(?i)(?<![a-z0-9])@?{_name_pattern(name)}(?![a-z0-9])\s*[,:!]?\s*",
                " ",
                out,
            )
    return " ".join(out.split())
