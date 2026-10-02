"""How Mika and Luna feel about the code, on their faces and in a few words.

Pre-written lines and real Live2D expressions, so this costs no LLM call and
never waits for one. Everything here only describes what really happened:

* bug        the browser check or the Python run found a real problem
* fixed      the one repair pass made it work
* failed     it is still broken after the repair (honest, never "it works")
* focus      writing or running has taken more than a couple of seconds
* tired      a long build (about 15 seconds or more): one short grumble

Faces are chosen from what each model actually has (the action registry):
the first name in the list that the character supports is played.
"""

from __future__ import annotations

import random
from typing import Optional

FACES: dict[str, tuple[str, ...]] = {
    "bug": ("pout", "suspicious", "surprised"),
    "fixed": ("sparkle", "cheer", "smile"),
    "failed": ("sad", "magic_fail"),
    "focus": ("close_eyes", "head_tilt", "suspicious"),
    "tired": ("sad", "side_eye", "pout"),
}

# Body language on top of the face (motions from each model's registry).
# The first one the character has is played.
MOTIONS: dict[str, tuple[str, ...]] = {
    "bug": ("magic_fail", "side_eye", "playful_kick"),
    "fixed": ("magic_heart", "cheer", "bounce", "nod"),
    "failed": ("open_arms", "shy_sway"),
}
# Little fidgets while a long build runs, so nobody looks frozen.
FIDGETS: tuple[str, ...] = ("shy_sway", "hat_tip", "head_tilt", "bounce", "nod")

LINES: dict[str, dict[str, list[str]]] = {
    "bug": {
        "mika": [
            "AHHH, a bug again?! Hold on, I'm squashing it!",
            "Nooo, a bug! Okay okay, fixing it right now!",
            "Ugh, the code is being dramatic. Give me a second!",
            "A bug?! In MY code?! Unacceptable. Fixing it!",
            "Wait wait wait, something broke. I've got this!",
            "My spell backfired! Hold on, re-casting!",
            "Who put a bug in here?! Luna, was that you?!",
            "Eek! Red text! Don't look, chat, I'm fixing it!",
        ],
        "luna": [
            "Hm. A bug. How predictable. Fixing it.",
            "One small error. Correcting it now.",
            "Ah. That line is wrong. One moment.",
            "A bug. Of course there is. Let me fix it.",
            "Not quite right yet. Repairing it.",
            "Mika would blame the computer. I'll just fix it.",
            "Hm. The code disagrees with me. It's wrong, of course.",
        ],
    },
    "fixed": {
        "mika": [
            "Bug defeated! Hehe, I'm amazing!",
            "Fixed it! Told you I had it!",
            "Yes! It works now!",
            "Hah! The bug never stood a chance!",
            "Fixed! Mika's magic wins again!",
        ],
        "luna": [
            "Fixed. As expected.",
            "There. Working properly now.",
            "Repaired. Much better.",
            "Done. That bug won't be back.",
        ],
    },
    "tired": {
        "mika": [
            "This one's a big one... almost there!",
            "So much code... my wand hand is tired!",
            "Still writing, chat, don't leave me!",
            "Ugh, why is this taking so long?! Typing faster!",
        ],
        "luna": [
            "This one is long. Patience.",
            "Still writing. Good things take time.",
            "Almost there. It's a big one.",
            "Patience, chat. Even I need a moment.",
        ],
    },
}

FOCUS_AFTER_SECONDS = 2.0
TIRED_AFTER_SECONDS = 15.0
FACE_REFRESH_SECONDS = 3.2  # expressions fade after a few seconds; keep it on


class LinePicker:
    """Random lines that never repeat the previous one for the same mood."""

    def __init__(self, rng: Optional[random.Random] = None):
        self.rng = rng or random.Random()
        self._last: dict[tuple[str, str], str] = {}

    def line(self, mood: str, character_id: str) -> str:
        pool = LINES.get(mood, {})
        options = pool.get(character_id) or pool.get("mika") or []
        if not options:
            return ""
        key = (mood, character_id)
        fresh = [o for o in options if o != self._last.get(key)] or options
        choice = self.rng.choice(fresh)
        self._last[key] = choice
        return choice


def face_for(mood: str, supports) -> str:
    """The first face for this mood the character's model supports."""
    for name in FACES.get(mood, ()):
        if supports(name):
            return name
    return ""


def motion_for(mood: str, supports) -> str:
    for name in MOTIONS.get(mood, ()):
        if supports(name):
            return name
    return ""
