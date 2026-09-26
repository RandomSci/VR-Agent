"""Chat text to typed game commands. Deterministic, allowlisted, no LLM.

Viewer text is only matched against fixed patterns. The result is one of the
typed commands in ``base`` (or None). Game names are resolved through the
Game Registry; names of games we do not have ("chess") are recognised so the
characters can say honestly that the game isn't available yet.
"""

from __future__ import annotations

import re
from typing import Optional

from .base import (
    ChangeGame,
    Command,
    HowToPlay,
    ListGames,
    NextRound,
    SetCategory,
    SetDifficulty,
    SetFirstPlayer,
    StartGame,
    StopGame,
)
from .registry import GameRegistry, normalise_name

MAX_COMMAND_WORDS = 12

# Games people ask for that are not installed. Used for honest replies only.
KNOWN_GAME_NAMES = (
    "tic tac toe",
    "tictactoe",
    "chess",
    "checkers",
    "uno",
    "minecraft",
    "fortnite",
    "roblox",
    "among us",
    "would you rather",
    "rock paper scissors",
    "hangman",
    "guess the word",
    "word guess",
    "memory",
    "pictionary",
    "poker",
    "blackjack",
    "sudoku",
    "wordle",
    "truth or dare",
    "20 questions",
    "twenty questions",
    "charades",
    "bingo",
    "mahjong",
    "scrabble",
    "monopoly",
    "connect four",
    "connect 4",
    "go fish",
    "hide and seek",
    "tag",
    "mario kart",
    "valorant",
    "league of legends",
    "dota",
    "mobile legends",
    "ml",
)

_LEAD = r"(?:(?:hey|yo|ok|okay|pls|please|plz|guys|girls)\s+)*(?:@?\w+\s*,\s*)?"
_START_VERB = r"(?:let'?s|lets|can (?:you|we|u)|could (?:you|we)|wanna|want to|we should|go|time to|i want (?:you|to)|pls|please)?\s*"
_LIST_RE = re.compile(
    r"(?:what|which|how many|list|any)\b.{0,20}\bgames?\b|\bgames?\b.{0,20}\b(?:can you|do you|you have|available|list)\b",
    re.I,
)
_STOP_RE = re.compile(
    r"^"
    + _LEAD
    + r"(?:pls |please )?(?:stop|end|quit|exit|cancel|finish)\s+(?:the\s+|this\s+)?(?:game|trivia|quiz|playing)\b|\bno more (?:trivia|games?|quiz)\b",
    re.I,
)
_NEXT_RE = re.compile(
    r"^"
    + _LEAD
    + r"(?:pls |please )?(?:next (?:round|question|one)|skip(?: (?:this|the|that) (?:one|question|round))?)\s*[!.]*$",
    re.I,
)
_CHANGE_RE = re.compile(
    r"\b(?:change|switch)(?: the)? games?\b|\b(?:another|different|new|other) game\b|\bplay something else\b|\bswitch to (?P<target>[\w' -]{2,30})",
    re.I,
)
_START_RE = re.compile(
    r"^"
    + _LEAD
    + _START_VERB
    + r"(?:play|start|begin|do)\s+(?:a\s+|some\s+|the\s+|us\s+)?(?P<name>[\w' -]{2,30}?)\s*(?:game|now|again|pls|please|with us)?\s*[!.?]*$",
    re.I,
)
_BARE_GAME_RE = re.compile(
    r"^(?P<name>[\w' -]{2,30}?)\s*(?:time|pls|please|now)?\s*[!.]*$", re.I
)
_DIFFICULTY_RE = re.compile(
    r"\bmake it (?P<rel>harder|easier|hard|easy)\b|\b(?P<abs>easy|medium|hard|mixed)\s+(?:mode|questions?|difficulty|level)\b|\b(?:harder|tougher) questions\b(?P<harder>)|\beasier questions\b(?P<easier>)",
    re.I,
)
_CATEGORY_RE = re.compile(
    r"\b(?P<cat>[a-z]{3,20})\s+(?:questions?|trivia|category)\b|\b(?:ask|questions?) about (?P<about>[a-z]{3,20})\b|\bcategory\s*[:=]?\s*(?P<named>[a-z]{3,20})\b",
    re.I,
)
_CATEGORY_ALIASES = {
    "planet": "space",
    "planets": "space",
    "astronomy": "space",
    "universe": "space",
    "animal": "animals",
    "nature": "animals",
    "country": "geography",
    "countries": "geography",
    "capitals": "geography",
    "maps": "geography",
    "tech": "technology",
    "computer": "technology",
    "computers": "technology",
    "coding": "technology",
    "programming": "technology",
    "cooking": "food",
    "foods": "food",
    "random": "any",
    "mixed": "any",
    "general": "general",
}


_RULES_RE = re.compile(
    r"\bhow (?:does|do|would|will) (?:it|this|that|the game|trivia|you play(?: it)?|we play(?: it)?|i play(?: it)?|that game|this game|\w+ (?:battle|game)) (?:work|go)\b"
    r"|\bhow (?:do|does|can) (?:i|we|you|chat|one) (?:play|join|answer|win)\b|\bhow to play\b"
    r"|\bwhat are the rules\b|\b(?:the |its |it's )?rules\?|\bexplain (?:the )?(?:game|rules|it)\b|\bhow does it work\b",
    re.I,
)
# "sure", "yes!", "let's start", "I'm in" ... only meaningful right after a game was offered.
_ACCEPT_RE = re.compile(
    r"^(?:(?:ok(?:ay)?|sure|yes+|yeah+|yep|yup|ya|yas+|alright|all right|fine|go|lets go|let'?s go|sounds good|why not|of course|absolutely|definitely|bet|ready|im ready|i'?m ready|i'?m in|im in|count me in|me too|do it|let'?s do it|lets do it|bring it on|go for it|i'?m interested|im interested|interested|start|start it|begin|let'?s start|lets start|let'?s begin|lets begin|let'?s play|lets play|play|start the game|start now|go ahead|i want to play|wanna play|hell yeah|heck yes|please|pls|plz)[\s,.!?]*)+$",
    re.I,
)
_BARE_START_RE = re.compile(
    r"^(?:(?:ok(?:ay)?|so|then|now|alright)[\s,]+)?(?:let'?s|lets|can we|we can|pls|please)?\s*(?:start|begin|play)(?:\s+(?:the game|a game|it|now|already|please|pls))*[\s!.?]*$",
    re.I,
)


def _mentioned_game(text: str, registry: GameRegistry) -> Optional[str]:
    lowered = f" {normalise_name(text)} "
    for factory in registry.enabled():
        names = {
            normalise_name(factory.info.id),
            normalise_name(factory.info.display_name),
            *(normalise_name(a) for a in factory.info.aliases),
        }
        if any(n and f" {n} " in lowered for n in names):
            return factory.info.id
    return None


def mentioned_game(text: str, registry: GameRegistry) -> Optional[str]:
    """Installed game named anywhere in ``text`` (for offers), or None."""
    return _mentioned_game(text, registry)


def _words(text: str) -> int:
    return len(text.split())


def _known_other_game(name: str) -> Optional[str]:
    cleaned = normalise_name(name)
    for known in KNOWN_GAME_NAMES:
        if (
            cleaned == known
            or cleaned.startswith(known + " ")
            or cleaned.endswith(" " + known)
        ):
            return known
    return None


def _resolve_game(
    name: str, registry: GameRegistry
) -> tuple[Optional[str], Optional[str]]:
    """(installed game id, unsupported name) for a spoken game name."""
    cleaned = normalise_name(name)
    cleaned = re.sub(
        r"\b(game|games|battle|please|pls|now|again|with us)\b", " ", cleaned
    )
    cleaned = " ".join(cleaned.split())
    if cleaned in ("", "a", "some", "something", "one"):
        return None, None
    for candidate in (name, cleaned):
        factory = registry.find(candidate)
        if factory:
            return factory.info.id, None
    other = _known_other_game(cleaned)
    if other:
        return None, other
    return None, None


def parse_command(
    text: str,
    registry: GameRegistry,
    players: Optional[dict[str, str]] = None,
    game_active: bool = False,
    categories: tuple[str, ...] = (),
    offered: Optional[str] = None,
) -> Optional[Command]:
    """``players`` maps lower-case names and aliases to character ids.
    ``offered`` is the game the room just offered or talked about; short
    replies like "sure" or "let's start" then start it."""
    raw = str(text or "").strip()
    if not raw or _words(raw) > MAX_COMMAND_WORDS + 4:
        return None
    lowered = raw.lower().replace("\u2019", "'")

    if not game_active:
        named = _mentioned_game(lowered, registry)
        if _RULES_RE.search(lowered) and (named or offered):
            return HowToPlay(named or offered)
        if offered and _ACCEPT_RE.match(lowered):
            return StartGame(game_id=offered)
        if _BARE_START_RE.match(lowered):
            return StartGame(game_id=offered)
    if _words(raw) > MAX_COMMAND_WORDS:
        return None

    if _STOP_RE.search(lowered):
        return StopGame()
    if _LIST_RE.search(lowered):
        return ListGames()

    change = _CHANGE_RE.search(lowered)
    if change:
        target = change.group("target") if change.groupdict().get("target") else None
        if target:
            game_id, other = _resolve_game(target, registry)
            return ChangeGame(
                game_id=game_id,
                requested_name=other or (None if game_id else target.strip()),
            )
        return ChangeGame()

    if game_active:
        if _NEXT_RE.search(lowered):
            return NextRound()
        difficulty = _DIFFICULTY_RE.search(lowered)
        if difficulty:
            if difficulty.group("rel"):
                rel = difficulty.group("rel").lower()
                return SetDifficulty({"hard": "harder", "easy": "easier"}.get(rel, rel))
            if difficulty.group("abs"):
                return SetDifficulty(difficulty.group("abs").lower())
            return SetDifficulty(
                "harder" if difficulty.group("harder") is not None else "easier"
            )
        if players:
            for name, player_id in players.items():
                pattern = rf"\b(?:let|have)\s+{re.escape(name)}\s+(?:answer|go|play)\s+first\b|\b{re.escape(name)}\s+(?:goes|answers|go|first)\s*(?:first)?\s*[!.]*$"
                if re.search(pattern, lowered):
                    return SetFirstPlayer(player_id)
        category = _CATEGORY_RE.search(lowered)
        if category:
            word = (
                category.group("cat")
                or category.group("about")
                or category.group("named")
                or ""
            ).lower()
            word = _CATEGORY_ALIASES.get(word, word)
            if word == "any" or word in categories:
                return SetCategory(word)

    start = _START_RE.search(lowered)
    if start:
        name = start.group("name").strip()
        if name in ("a game", "game", "games", "a", "some"):
            return StartGame()
        game_id, other = _resolve_game(name, registry)
        if game_id or other:
            return StartGame(game_id=game_id, requested_name=other)
        if re.fullmatch(r"(?:a |some )?(?:game|games)", name):
            return StartGame()
        return None
    if re.search(r"\b(?:start|play|begin)\s+(?:a|some)?\s*game\b", lowered):
        return StartGame()

    bare = _BARE_GAME_RE.match(lowered)
    if bare and _words(lowered) <= 3:
        game_id, _other = _resolve_game(bare.group("name"), registry)
        if game_id:
            return StartGame(game_id=game_id)
    return None
