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


# "build faster", "build it bigger", "make it gold": about the build going on, not a new one
NOT_A_NEW_BUILD = re.compile(
    r"(?:faster|quicker|quickly|slower|more|again|better|it\b|that\b|this\b|the rest|on\b|it'?s)", re.I)


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
    if len(rest) < 3 or (loose and NOT_A_BUILD.match(rest)) or NOT_A_NEW_BUILD.match(rest):
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
             r"i want to see|i wanna see|let me see|let us see|can we see|show|"
             r"(?:can|could|may) (?:i|we|u|you) (?:please )?(?:see|visit|look at|go to|check out)|"
             r"(?:let'?s|lets|we should|i'?d like to) (?:go |fly )?(?:see|visit|look at|to|check out)|"
             r"where(?:'?s| is) the")


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


# ---------------------------------------------------------------- the build going on
# "make it more colorful", "add gold on it", "build me a golden tower" while
# a tower is being built: the build going on changes, no new build is started.
MATERIAL_WORDS = {
    "gold": "gold_block", "golden": "gold_block", "diamond": "diamond_block", "diamonds": "diamond_block",
    "emerald": "emerald_block", "iron": "iron_block", "netherite": "netherite_block", "copper": "copper_block",
    "glass": "glass", "quartz": "quartz_block", "obsidian": "obsidian", "amethyst": "amethyst_block",
    "lapis": "lapis_block", "redstone": "redstone_block", "brick": "bricks", "bricks": "bricks",
    "wood": "oak_planks", "wooden": "oak_planks", "stone": "stone_bricks", "sandstone": "smooth_sandstone",
    "prismarine": "prismarine_bricks", "ice": "packed_ice", "snow": "snow_block", "glowstone": "glowstone",
    "red": "red_concrete", "orange": "orange_concrete", "yellow": "yellow_concrete", "green": "lime_concrete",
    "lime": "lime_concrete", "blue": "blue_concrete", "cyan": "cyan_concrete", "purple": "purple_concrete",
    "pink": "pink_concrete", "magenta": "magenta_concrete", "white": "white_concrete", "black": "black_concrete",
    "gray": "gray_concrete", "grey": "gray_concrete", "brown": "brown_concrete",
}
RAINBOW = ("red_concrete", "orange_concrete", "yellow_concrete", "lime_concrete", "light_blue_concrete",
           "blue_concrete", "purple_concrete", "magenta_concrete")
_COLORFUL = re.compile(r"\b(?:colou?rful|more colou?rs?|rainbow|colou?r it|paint it|brighter|more vibrant|so plain|"
                       r"too (?:simple|plain|boring|grey|gray))\b", re.I)
_CHANGE = re.compile(r"\b(?:make it|make the|add|adding|put|use|with|change|paint|turn it|cover|decorate|trim|"
                     r"more|golden|all|too simple|too plain)\b", re.I)
_ACCENT = re.compile(r"\b(?:add|adding|put|trim|details?|accents?|decorat\w*|on it|on top|stripes?)\b", re.I)


def restyle_asked(text: str) -> tuple[list[str], list[str]]:
    """What the build going on should be made of now: (main blocks, trim blocks).
    'make it more colorful' -> (RAINBOW, []); 'add gold on it' -> ([], [gold]);
    'make it all diamond' -> ([diamond], []); both lists empty when not asked."""
    low = (text or "").lower()
    colorful = bool(_COLORFUL.search(low))
    blocks = list(dict.fromkeys(MATERIAL_WORDS[w] for w in re.findall(r"[a-z]+", low) if w in MATERIAL_WORDS))
    if not colorful and not (blocks and _CHANGE.search(low)):
        return [], []
    main = list(RAINBOW) if colorful else []
    trim: list[str] = []
    if blocks:
        if colorful or (_ACCENT.search(low) and not re.search(r"\b(?:all|whole|entire|make it)\b", low)):
            trim = blocks
        else:
            main = blocks
    return main, trim


def tour_asked(text: str) -> bool:
    """'Mika fly around your whole world', 'give us a tour', 'show me everything you built'."""
    return _asked(text, r"(?:fly|go|walk|look) around|(?:give (?:me|us) |do )?a tour|tour\b|show (?:me|us) (?:around|"
                        r"everything|all|your (?:whole )?world)|(?:i (?:wanna|want to) see|show me) (?:the |your )?(?:whole|"
                        r"entire) (?:world|map|kingdom)") or bool(re.search(r"\bfly around (?:your|the) (?:whole )?world\b",
                                                                             text or "", re.I))


def friend_asked(text: str, friend: str) -> bool:
    """'where's Luna? come to her', 'go to Luna', 'find Luna' (to the camera girl)."""
    f = re.escape(friend)
    return bool(re.search(rf"\b(?:where(?:'?s| is| are you)\s+{f}|(?:come|go|fly) (?:to|back to|over to|near) "
                          rf"(?:{f}|her)|find {f}|visit {f}|stand (?:next to|with|near) (?:{f}|her)|look at {f})\b",
                          text or "", re.I))


_REVISIT_VERB = (r"(?:look (?:at|over|back at)|show (?:me|us)|go (?:back )?to|fly (?:back )?to|see|visit|check(?: out)?|"
                 r"take (?:me|us) to|i (?:want|wanna) (?:to )?see|let'?s see|can (?:we|i) see|back to)")
_WHICH = re.compile(r"\b(first|last|previous|other|old|older|earlier|you built|we built|they built|my|mine|"
                    r"for me)\b", re.I)


def revisit_asked(text: str) -> bool:
    """'look over the first tower you built', 'show me my castle again'."""
    return bool(re.search(_REVISIT_VERB, text or "", re.I))


def which_build(text: str, builds: list[dict], author: str = "") -> int:
    """Which finished build a viewer means (-1: none). 'first' -> the first,
    'last/previous/that' -> the latest, 'my' -> theirs, a title word -> that one."""
    low = (text or "").lower()
    if not builds or not revisit_asked(low):
        return -1
    words = set(re.findall(r"[a-z]{4,}", low)) - {"look", "show", "over", "back", "first", "last", "built", "build",
                                                   "want", "wanna", "check", "visit", "again", "please", "that", "this",
                                                   "with", "your", "mika", "luna", "they", "there", "where"}
    best, score = -1, 0
    for i, b in enumerate(builds):
        title = set(re.findall(r"[a-z]{4,}", str(b.get("title", "")).lower()))
        overlap = len(words & title)
        if overlap > score or (overlap and overlap == score):  # ties: the newer one
            best, score = i, overlap
    which = _WHICH.search(low)
    if which:
        kind = which.group(1)
        pool = [i for i in range(len(builds)) if best < 0 or score == 0 or
                set(re.findall(r"[a-z]{4,}", str(builds[i].get("title", "")).lower())) & words] or list(range(len(builds)))
        if kind in ("my", "mine", "for me") and author:
            mine = [i for i in pool if str(builds[i].get("who", "")).lower() == author.lower()]
            pool = mine or pool
        return pool[0] if kind == "first" else pool[-1]
    return best


_THERE = re.compile(r"\b(?:visit|see|show|look at|go|fly|take (?:me|us)|check)\b.{0,20}\b(?:it|there|that|that place|"
                    r"this one)\b", re.I)


def there_asked(text: str) -> bool:
    """'Mika let's visit it now!', 'go there', 'show it to us' (what "it" is comes from the chat before)."""
    return bool(_THERE.search(text or ""))


_HERE = re.compile(r"\b(?:where (?:you|u|yo\w*|ya) (?:stand|are|r|standing|at)|right here|over here|here|right now "
                   r"where|next to you|in front of you|on this spot|on the spot|this spot|right there)\b", re.I)


def here_asked(text: str) -> bool:
    """'build another 10 where you stand right now', 'build it right here'."""
    return bool(_HERE.search(text or ""))
