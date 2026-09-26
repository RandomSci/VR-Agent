"""Deterministic viewer action-intent parsing.

Turns chat such as "can you smile?", "wave at me!" or "kaway ka naman" into a
canonical intent name ("smile", "wave"). The intent is then resolved against
the CharacterCapabilities registry, so only allowlisted actions can ever run.

No LLM call is made here. Parsing is a couple of regexes per message, which
keeps obvious commands free and fast. Viewer text is only ever compared
against fixed patterns; nothing from it is used as an identifier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .capabilities import CharacterAction, CharacterCapabilities

# Canonical intent -> verb/noun pattern. English plus common Tagalog/Taglish
# forms, since many viewers of this channel write in Taglish.
_INTENT_PATTERNS: dict[str, str] = {
    "smile": r"smile|smiling|ngiti|ngumiti|ngitian",
    "happy": r"(?:look|be|act) (?:happy|cheerful|glad)|happy face",
    "laugh": r"laugh|tawa|tumawa",
    "excited": r"(?:look|be|act) excited|sparkl\w*|excited face",
    "blush": r"blush\w*|namula|mamula",
    "shy": r"(?:look|be|act) shy|shy face|mahiya|nahihiya",
    "surprised": r"(?:look|be|act) (?:surprised|shocked)|surprised face|shocked face|gulat",
    "sad": r"(?:look|be|act) sad|sad face|malungkot",
    "angry": r"(?:look|be|act) (?:angry|mad|annoyed)|pout\w*|angry face|galit",
    "close_eyes": r"close your eyes|shut your eyes|pikit|pumikit",
    "sleep": r"go to sleep|sleepy face|matulog",
    "wave": r"wave|waving|kaway|kumaway|kawayan",
    "greet": r"greet|say hi|say hello|batiin|bumati",
    "hat": r"tip your hat|hat tip|touch your hat|fix your hat",
    "clap": r"clap\w*|palakpak|pumalakpak",
    "nod": r"nod\w*|tango|tumango",
    "yes": r"nod yes",
    "pose": r"pose|posing|mag ?pose",
    "cute": r"(?:be|act|look) cute|cute pose|pa ?cute",
    "heart": r"(?:make|draw|do|send|show) (?:us |me )?(?:a )?heart|heart magic|magic heart|puso",
    "magic": r"(?:do|cast|show) (?:some |a |us |me )*(?:magic|spell)|magic trick",
    "magic_fail": r"fail(?:ed)? (?:a )?spell",
    "rabbit": r"rabbit|bunny|kuneho",
    "dance": r"dance|dancing|sayaw|sumayaw",
    "bow": r"\bbow\b|yumuko",
    "wink": r"wink\w*|kindat|kumindat",
    "thumbs_up": r"thumbs? up",
    "peace": r"peace sign",
    "jump": r"jump\w*|talon|tumalon",
    "spin": r"spin\w*|turn around|ikot|umikot",
}
_COMPILED = {
    intent: re.compile(rf"(?<![a-z])(?:{pattern})(?![a-z])", re.IGNORECASE)
    for intent, pattern in _INTENT_PATTERNS.items()
}

# Phrases that make the message a request to the character rather than a
# statement ("I smiled at my cat").
_REQUEST_RE = re.compile(
    r"(?<![a-z])(?:can you|could you|would you|will you|can u|could u|pls|plz|please|"
    r"show (?:me|us)|let me see|i wanna see|i want to see|do (?:a|an|the|some)|give (?:me|us)|"
    r"try to|try and|you should|go on|again|"
    r"naman|nga|paki|sige|ka)(?![a-z])",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(
    r"(?<![a-z])(?:don'?t|do not|never|stop|no more|wag|huwag|ayoko)(?![a-z])",
    re.IGNORECASE,
)
_LEADING_MENTION_RE = re.compile(r"^(?:@\S+\s+)+")
_MAX_PARSE_CHARS = 200


@dataclass(frozen=True)
class ActionIntent:
    """Outcome of parsing one viewer message."""

    requested: Optional[str]  # canonical intent the viewer asked for
    action: Optional[CharacterAction]  # action that will actually be performed
    supported: bool  # True when `action` is exactly what was asked for

    @property
    def is_alternative(self) -> bool:
        return (
            self.requested is not None
            and self.action is not None
            and not self.supported
        )

    @property
    def is_unsupported(self) -> bool:
        return self.requested is not None and self.action is None


NO_INTENT = ActionIntent(requested=None, action=None, supported=False)


def detect_intent(text: str) -> Optional[str]:
    """Return the canonical intent a message requests, or None."""
    cleaned = _LEADING_MENTION_RE.sub("", (text or "").strip())[:_MAX_PARSE_CHARS]
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if _NEGATION_RE.search(lowered):
        return None

    matches: list[tuple[int, str]] = []
    for intent, pattern in _COMPILED.items():
        found = pattern.search(lowered)
        if found:
            matches.append((found.start(), intent))
    if not matches:
        return None
    matches.sort()
    position, intent = matches[0]

    starts_with_verb = position <= 2  # "smile!", "wave at me", "a pose pls"
    is_request = starts_with_verb or bool(_REQUEST_RE.search(lowered))
    if not is_request and "?" in lowered:
        is_request = True
    if not is_request:
        return None
    # A bare command must stay short; long chatter merely mentioning a verb
    # ("I smiled all day at work and ...") is not a command.
    if (
        starts_with_verb
        and not _REQUEST_RE.search(lowered)
        and len(lowered.split()) > 8
    ):
        return None
    return intent


def resolve_intent(
    text: str, capabilities: Optional[CharacterCapabilities]
) -> ActionIntent:
    """Parse a message and map it onto the character's real capabilities."""
    intent = detect_intent(text)
    if intent is None:
        return NO_INTENT
    if capabilities is None:
        return ActionIntent(requested=intent, action=None, supported=False)
    direct = capabilities.action_for_intent(intent)
    if direct:
        return ActionIntent(requested=intent, action=direct, supported=True)
    alternative = capabilities.alternative_for_intent(intent)
    if alternative:
        return ActionIntent(requested=intent, action=alternative, supported=False)
    return ActionIntent(requested=intent, action=None, supported=False)


def intent_verb(intent: str) -> str:
    """Human wording for prompts, e.g. 'close_eyes' -> 'close your eyes'."""
    return {
        "close_eyes": "close your eyes",
        "thumbs_up": "give a thumbs up",
        "peace": "make a peace sign",
        "hat": "tip your hat",
        "greet": "greet them",
        "yes": "nod yes",
        "heart": "make a heart",
        "magic": "do some magic",
        "magic_fail": "fail a spell",
        "rabbit": "show your rabbit",
        "happy": "look happy",
        "excited": "look excited",
        "surprised": "look surprised",
        "sad": "look sad",
        "angry": "look angry",
        "shy": "act shy",
        "cute": "act cute",
        "pose": "do a pose",
        "sleep": "go to sleep",
    }.get(intent, intent.replace("_", " "))
