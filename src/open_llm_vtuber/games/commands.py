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
    + r"(?:pls |please )?(?:stop|end|quit|exit|cancel|finish)\s+(?:the\s+|this\s+)?(?:game|trivia|quiz|playing|tic tac toe|rock paper scissors)\b|\bno more (?:trivia|games?|quiz)\b",
    re.I,
)
# "girls, play ...", "both of you", "mika vs luna", "play each other"
_VERSUS_RE = re.compile(
    r"\b(?:girls|both of you|you two|you both|each other|against each other|one another|vs\.?|versus)\b",
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
    "anime": "movies",
    "movie": "movies",
    "film": "movies",
    "films": "movies",
    "cartoon": "movies",
    "cartoons": "movies",
    "sport": "sports",
    "song": "music",
    "songs": "music",
    "kpop": "music",
    "random": "any",
    "mixed": "any",
    "general": "general",
}


_RULES_RE = re.compile(
    r"\bhow (?:does|do|would|will) (?:it|this|that|the game|trivia|you play(?: it)?|we play(?: it)?|i play(?: it)?|that game|this game|[\w -]{2,24} (?:battle|game)) (?:work|go)\b"
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


def _players_named(text: str, players: Optional[dict[str, str]]) -> list[str]:
    """Character ids named in ``text``, in order of appearance."""
    if not players:
        return []
    found: list[tuple[int, str]] = []
    lowered = text.lower()
    for name, player_id in players.items():
        match = re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", lowered)
        if match and player_id not in [p for _, p in found]:
            found.append((match.start(), player_id))
    return [p for _, p in sorted(found)]


def _start_options(
    text: str, players: Optional[dict[str, str]]
) -> dict[str, Optional[str]]:
    """Who plays: the girls against each other, or chat against one of them."""
    named = _players_named(text, players)
    lowered = text.lower()
    challenge = re.search(
        r"\b(?:challenge|vs\.?|versus|against|play with|play against)\b", lowered
    )
    if len(named) >= 2 or _VERSUS_RE.search(lowered):
        return {
            "mode": "characters",
            "first": named[0] if named else None,
            "opponent": None,
        }
    if len(named) == 1:
        # "Luna, play tic tac toe with us" / "chat vs Mika": chat plays that character.
        return {"mode": "chat", "opponent": named[0], "first": None}
    if challenge and "chat" in lowered:
        return {"mode": "chat", "opponent": None, "first": None}
    return {"mode": None, "opponent": None, "first": None}


def _with_options(command: StartGame, text: str, players) -> StartGame:
    options = _start_options(text, players)
    return StartGame(
        game_id=command.game_id,
        requested_name=command.requested_name,
        mode=options["mode"],
        opponent=options["opponent"],
        first=options["first"],
    )


_CHALLENGE_RE = re.compile(
    r"(?P<a>\w+)\s*,?\s*(?:please |pls )?(?:challenge|challenges|play|plays|vs\.?|versus|against)\s+(?P<b>\w+)\s+(?:to|in|at|on)\s+(?:a\s+|some\s+|the\s+)?(?P<name>[\w' -]{2,30}?)\s*(?:game|match|battle)?\s*[!.?]*$",
    re.I,
)


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
        rules = _RULES_RE.search(lowered) or (
            named
            and re.search(r"\bhow\b.{0,40}\b(?:work|works|play|played)\b", lowered)
        )
        if rules and (named or offered):
            return HowToPlay(named or offered)
        challenge = _CHALLENGE_RE.search(lowered)
        if challenge and players:
            a = players.get(challenge.group("a").lower())
            b = players.get(challenge.group("b").lower())
            game_id, other = _resolve_game(challenge.group("name"), registry)
            if a and b and a != b and (game_id or other):
                return StartGame(
                    game_id=game_id, requested_name=other, mode="characters", first=a
                )
        if offered and _ACCEPT_RE.match(lowered):
            return _with_options(StartGame(game_id=offered), lowered, players)
        if _BARE_START_RE.match(lowered):
            return _with_options(StartGame(game_id=offered), lowered, players)
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
            return _with_options(StartGame(), lowered, players)
        game_id, other = _resolve_game(name, registry)
        if game_id or other:
            return _with_options(
                StartGame(game_id=game_id, requested_name=other), lowered, players
            )
        if re.fullmatch(r"(?:a |some )?(?:game|games)", name):
            return _with_options(StartGame(), lowered, players)
    named_game = _mentioned_game(lowered, registry)
    if named_game and re.search(
        r"\b(?:play|start|begin|let'?s|lets|wanna|want to)\b", lowered
    ):
        # "Girls, play tic tac toe with each other", "chat vs Luna in rps, let's go"
        return _with_options(StartGame(game_id=named_game), lowered, players)
    if start:
        return None
    if re.search(r"\b(?:start|play|begin)\s+(?:a|some)?\s*game\b", lowered):
        return _with_options(StartGame(), lowered, players)

    bare = _BARE_GAME_RE.match(lowered)
    if bare and _words(lowered) <= 4:
        game_id, _other = _resolve_game(bare.group("name"), registry)
        if game_id:
            return StartGame(game_id=game_id)
    return None
