"""The line a character says the moment a build starts.

Writing code takes several seconds. Without this the stream goes silent
while the model works, which reads as frozen. The acknowledgment is spoken
at the same time as the code is written, costs one short TTS line and no LLM
call, and only ever says what is actually happening (a build has started).
"""

from __future__ import annotations

import random
import re
from typing import Optional

# {name} is the viewer, {subject} is what is being built.
NEW_LINES = {
    "mika": [
        "Okay {name}, building {subject} now!",
        "On it, {name}! Writing {subject} right now.",
        "Ooh, {subject}! Give me a moment, {name}.",
    ],
    "luna": [
        "Alright {name}, I'm writing {subject} now.",
        "Got it, {name}. Starting on {subject}.",
        "One moment, {name}. Building {subject}.",
    ],
}
CHANGE_LINES = {
    "mika": [
        "Okay {name}, changing it now!",
        "On it, {name}! Updating the code.",
        "Ooh, good idea {name}! Changing it.",
    ],
    "luna": [
        "Alright {name}, updating it now.",
        "Got it, {name}. Changing the code.",
        "One moment, {name}. Making that change.",
    ],
}
FALLBACK_SUBJECT = "it"

# Only for cleaning text that will be SPOKEN (no symbols read aloud).
_UNSPEAKABLE = re.compile(r"[^\w\s'.,!?-]+", re.UNICODE)


def speakable_name(display_name: str) -> str:
    name = str(display_name or "").strip().lstrip("@")
    name = _UNSPEAKABLE.sub(" ", name)
    name = " ".join(name.split())[:32]
    return name or "friend"


def speakable_subject(subject: str) -> str:
    text = _UNSPEAKABLE.sub(" ", str(subject or ""))
    text = " ".join(text.split()).strip(" .,!?")[:60]
    return text or FALLBACK_SUBJECT


def acknowledgment(
    character_id: str,
    viewer_name: str,
    subject: str,
    fresh: bool,
    rng: Optional[random.Random] = None,
) -> str:
    rng = rng or random
    pool = (NEW_LINES if fresh else CHANGE_LINES).get(
        character_id, (NEW_LINES if fresh else CHANGE_LINES)["mika"]
    )
    return rng.choice(pool).format(
        name=speakable_name(viewer_name), subject=speakable_subject(subject)
    )
