"""Deterministic viewer requests the stage itself can perform.

"walk towards the center", "Luna go to the left", "come closer", "go back
to your spot", "jump!", "dance for us", "look at the frog", "Mika make the
frog disappear". A few regexes, no LLM. The router performs a request right
away through the Stage Director or Interactions, then the Conversation
Director tells the character exactly what is happening, so her words match
the screen: she never says "I can't walk" while walking.

Only clearly imperative phrasing counts; chatter that merely mentions a
word ("I walked my dog to the park") stays ordinary conversation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_NEGATION = re.compile(
    r"(?<![a-z])(?:don'?t|do not|never|stop|no more|wag|huwag)(?![a-z])"
)
_REQUEST = re.compile(
    r"(?<![a-z])(?:can you|could you|would you|will you|can u|pls|plz|please|try to|"
    r"i want you to|you should|go on|now|naman|nga|paki)(?![a-z])"
)
_MOVE_VERB = r"(?:walk|go|move|come|step|head|float|stroll|run|get|stand|scoot|shift)"
_ZONE_WORDS = {
    "far left": "far_left",
    "far right": "far_right",
    "left": "left",
    "right": "right",
    "center": "center",
    "centre": "center",
    "middle": "center",
    "front": "center",
    "closer": "center",
    "here": "center",
}
_MOVE_RE = re.compile(
    rf"(?<![a-z]){_MOVE_VERB}(?:\s+(?:over|back|up|down|a bit|a little|more|slowly))*"
    r"(?:\s+(?:to|towards?|toward|into|in|on|at|over to))?"
    r"(?:\s+(?:the|your|my))?\s+(?P<where>far left|far right|left|right|center|centre|middle|front|closer|here|[a-z_]+)"
    r"(?:\s+(?:side|spot|part|area|of the stage|of the screen))?"
)
_HOME_RE = re.compile(
    r"(?<![a-z])(?:go|get|walk|move|head|come)\s+back(?:\s+to\s+(?:your|ur)\s+(?:spot|place|position))?(?![a-z])"
    r"|(?:back|return)\s+to\s+(?:your|ur)\s+(?:spot|place|position)"
)
_HOP_RE = re.compile(r"(?<![a-z])(?:jump|hop|bounce|talon|tumalon)(?:s|ing)?(?![a-z])")
_DANCE_RE = re.compile(r"(?<![a-z])(?:dance|dancing|sayaw|sumayaw)(?![a-z])")
_LOOK_RE = re.compile(
    r"(?<![a-z])(?:look|glance|stare)\s+(?:at|over at|toward|towards|to)\s+(?:the\s+)?(?P<what>[a-z_ ]{2,30})"
)
_MAGIC_RE = re.compile(
    r"(?<![a-z])(?:(?:use|cast|do|try)\s+(?:your\s+|some\s+|a\s+)?(?:magic|spell)\s+on\s+(?:the\s+|that\s+)?(?P<a>[a-z_ ]{2,30})"
    r"|make\s+(?:the\s+|that\s+)?(?P<b>[a-z_ ]{2,30}?)\s+(?:disappear|vanish|go away|glow|shine|light up)"
    r"|(?:zap|poof|vanish|enchant)\s+(?:the\s+|that\s+)?(?P<c>[a-z_ ]{2,30}))"
)


@dataclass(frozen=True)
class WorldRequest:
    kind: str  # move | home | hop | dance | look | magic
    phrase: str  # human wording for the prompt: "walk to the center"
    zone: str = ""
    target: str = ""  # free text naming a thing or person ("the frog", "me")


def _imperative(lowered: str, match_start: int) -> bool:
    return (
        match_start <= 12
        or bool(_REQUEST.search(lowered))
        or "?" in lowered
        or "!" in lowered
    )


def parse_request(
    text: str, named_zones: dict[str, str] | None = None
) -> Optional[WorldRequest]:
    """A request the stage can perform, or None for ordinary chat.

    ``named_zones`` maps words to scene zones ("pond" -> "pond").
    """
    lowered = " ".join(str(text or "").lower().split())
    lowered = re.sub(r"^(?:@\S+\s+)+", "", lowered)
    lowered = re.sub(r"^(?:hey\s+|hi\s+|yo\s+)?(?:mika|luna)[,!:]?\s+", "", lowered)
    if not lowered or len(lowered) > 100 or _NEGATION.search(lowered):
        return None
    named = named_zones or {}

    home = _HOME_RE.search(lowered)
    if home and _imperative(lowered, home.start()):
        return WorldRequest("home", "go back to your spot")

    magic = _MAGIC_RE.search(lowered)
    if magic and _imperative(lowered, magic.start()):
        target = (
            magic.group("a") or magic.group("b") or magic.group("c") or ""
        ).strip()
        verb = "glow" if re.search(r"glow|shine|light up", lowered) else "vanish"
        return WorldRequest(
            "magic",
            f"use magic on the {target}"
            if verb == "vanish"
            else f"make the {target} glow",
            target=target,
        )

    move = _MOVE_RE.search(lowered)
    if move and _imperative(lowered, move.start()):
        where = move.group("where").strip()
        zone = _ZONE_WORDS.get(where) or named.get(where)
        if zone:
            label = (
                where
                if where in named
                else {
                    "far_left": "the far left",
                    "far_right": "the far right",
                    "left": "the left",
                    "right": "the right",
                    "center": "the center",
                }[zone]
            )
            phrase = (
                "come closer" if where in ("closer", "here") else f"move to {label}"
            )
            return WorldRequest("move", phrase, zone=zone)

    look = _LOOK_RE.search(lowered)
    if look and _imperative(lowered, look.start()):
        return WorldRequest(
            "look",
            f"look at {look.group('what').strip()}",
            target=look.group("what").strip(),
        )

    hop = _HOP_RE.search(lowered)
    if hop and _imperative(lowered, hop.start()) and len(lowered.split()) <= 10:
        return WorldRequest("hop", "jump")

    dance = _DANCE_RE.search(lowered)
    if dance and _imperative(lowered, dance.start()) and len(lowered.split()) <= 10:
        return WorldRequest("dance", "dance")
    return None
