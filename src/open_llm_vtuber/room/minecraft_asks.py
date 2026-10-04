"""What a chat message ASKS for, without misfiring on normal sentences.

Every action used to fire on a keyword anywhere in a message: "first place!"
started a build, "my internet is lagging" ran the stuck rescue, "this channel
will blow up" placed TNT, "number 1 fan" made the network read a 1. Now an
action needs a request: the action word at the start of the message, after
a name or a polite lead ("Mika build...", "can you race", "pls TNT",
"I want to see the castle"), or the message is just the action ("TNT!").
"""

from __future__ import annotations

import re

# words that may come before the action: names, politeness, "can you", "let's"
_LEAD_WORD = (
    r"(?:hey|hi|yo|ok|okay|oi|please|pls|plz|now|and|then|so|also|both|girls|guys|ladies|you two|u two|"
    r"mika|luna|chat|can you|could you|will you|would you|can u|could u|you should|u should|you guys should|"
    r"pretty please|go|lets|let'?s|let us|i want you to|i want u to|i wanna see you|i want to see you|"
    r"try to|you have to|you need to|time to|how about you|why not|maybe|quick|quickly|just|i dare you to|"
    r"i challenge you to|should|must|gotta|need to)"
)
LEAD = rf"^\W*(?:{_LEAD_WORD}\b[\s,!.:-]*)*"
_NOT = re.compile(r"\b(?:not|never|don'?t|dont|no|stop|isn'?t|aren'?t|wasn'?t|without)\b", re.I)


def _asked(text: str, action: str) -> bool:
    return bool(re.search(LEAD + rf"(?:{action})", text or "", re.I))


BUILD_VERB = (
    r"build|construct|create|make|craft|add|dig|place|put up|put|design|"
    r"how about (?:building |making )?(?:a|an|the|some|two|three)\b"
)
# what follows "make/put/place/add" in everyday chat, not a build: "make sure
# to subscribe", "put your hands up", "add me on discord", "make a wish"
NOT_A_BUILD = re.compile(
    r"(?:it|its|it'?s|that|this|those|these|sure|your|ur|yours|me|us|him|her|them|my day|my|our|"
    r"some noise|noise|a wish|wishes|a shoutout|shoutouts?|bets?|a bet|money|friends?|sense|fun|time|way|room|"
    r"love|peace|progress|a mistake|mistakes?|a comment|comments?|a video|videos?|content|a deal|plans?|"
    r"out|up|in|on|down|off|over|a joke|jokes|a point|points|history|an effort|efforts?|a choice|"
    r"a difference|the most|the best|it count|do)\b",
    re.I,
)


def wants_build(text: str) -> bool:
    """'Mika build a dragon', 'can you make two towers?', 'create a pumpkin',
    'dig a hole', 'how about a pirate ship' (not 'first place!', 'I dig it',
    'how did you build that', 'make sure to subscribe', 'put your hands up')."""
    text = text or ""
    match = re.search(LEAD + rf"(?:{BUILD_VERB})\b\s*(.*)", text, re.I)
    if not match:
        return False
    rest = match.group(1).strip(" !.?,")
    verb = re.search(rf"(?:{BUILD_VERB})\b", text[match.start():], re.I)
    loose = bool(verb) and verb.group(0).lower() in ("make", "put", "place", "add", "craft", "design", "create")
    if len(rest) < 3 or (loose and NOT_A_BUILD.match(rest)):
        return False
    return not re.fullmatch(r"(?:it|that|this|me|us|them|him|her|sure|ok)\W*", rest, re.I)


def wants_tnt(text: str) -> bool:
    """'TNT!', 'Luna blow it up', 'can you use tnt' (not 'this will blow up')."""
    action = (r"(?:use |place |get |light |drop |set off |do |some |more )?(?:the )?(?:tnt|t\.n\.t)|"
              r"blow (?:it|something|stuff|that|this|things|them|everything)? ?up|explode (?:it|something|that)|"
              r"kaboom|boom time|make (?:it|something) explode")
    return _asked(text, action)


def wants_race(text: str) -> bool:
    """'race!', 'you two should RACE', 'Mika race Luna' (not 'embrace it', 'the human race')."""
    return _asked(text, r"(?:have a |do a |start a |sky )?race\b|racing\b")


SHOW_VERB = (r"see|show (?:me |us )?|look at|go to|go see|fly to|visit|take (?:me|us) to|check out|"
             r"i want to see|i wanna see|let me see|let us see|can we see|show")


def show_asked(text: str) -> bool:
    return _asked(text, rf"(?:{SHOW_VERB})\b")


def further_asked(text: str) -> bool:
    """'zoom out', 'go a little further', 'show the whole thing' (not 'make it higher')."""
    action = (r"zoom out|(?:go|move|fly|step|back) (?:a (?:little|bit) |a little bit )?(?:further|farther|back|higher|"
              r"up|away)|(?:a (?:little|bit) )?(?:further|farther) (?:away|back|out|view)|back up|wider(?: view)?|from (?:afar|above)|bird'?s? eye|"
              r"(?:show|see) (?:the |it )?(?:whole thing|full view|everything)|full view|bigger view")
    return _asked(text, action)


def digit_asked(text: str) -> str:
    """'draw 7', 'Mika predict a 3', 'test number 5' -> '7' ('' when not asked;
    not 'number 1 fan', 'show me 2 dragons')."""
    match = re.search(LEAD + r"(?:draw|write|predict|guess|read|test(?: it)?(?: with| on)?|try)\s+"
                      r"(?:a |an |the |number |digit |the number )?([0-9])\b(?!\s*(?:dragons|towers|blocks|hours|"
                      r"minutes|times|of)\b)", text or "", re.I)
    return match.group(1) if match else ""


def says_stuck(text: str) -> bool:
    """'you're stuck', 'Mika is glitching', 'stream frozen?', 'stuck' (not 'my
    internet is lagging', 'you're not stuck', 'build a frozen castle')."""
    text = (text or "").strip()
    bad = (r"stuck|frozen|freez\w*|glitch\w*|bugg?ed|bugging|lagg?ing|laggy|not moving|can'?t move|shaking|"
           r"jitter\w*|teleporting back|spinning|broken")
    # whole words only ("it" was found inside "submit"), and only about the
    # girls or the picture ("my game keeps lagging" is the viewer's own game)
    subject = (r"\b(?:you|u|ya|you'?re|ur|youre|she|she'?s|shes|mika|luna|they|they'?re|theyre|both|"
               r"the camera|camera|cam|the stream|stream|the screen|screen|the view|view|it'?s|its|"
               r"her|the girls|girls)\b")
    if re.search(subject + r"\s+(?:are|is|r|'re|'s|look|looks|seem|seems|got|get|getting|keep|keeps|still|so|"
                 r"really|kinda|totally|completely|literally)?\s*(?:\w+\s+){0,2}?(?:" + bad + r")\b", text, re.I):
        clause = re.search(subject + r"\s+(.{0,30}?)(?:" + bad + r")", text, re.I)
        return not (clause and _NOT.search(clause.group(1)))
    # a short message that is only the complaint: "stuck?", "GLITCHING", "lag lol"
    words = re.findall(r"[a-z']+", text.lower())
    return 0 < len(words) <= 3 and bool(re.search(r"\b(?:" + bad + r"|lag)\b", text, re.I)) and not _NOT.search(text)
