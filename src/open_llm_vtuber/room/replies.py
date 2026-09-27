"""Deterministic spoken replies for game commands and template game lines.

These sentences come from code and the character yaml, not from an LLM, so
answering "what games can you play?" or "play chess" costs no LLM request.
"""

from __future__ import annotations

import random
from typing import Any, Optional

from .profiles import CharacterProfile

_DEFAULT_LINES = {
    "intro": ("Trivia Battle! Let's go!", "Game time!"),
    "tictactoe_intro": ("Tic-Tac-Toe! Chat, you're X!", "Tic-Tac-Toe time!"),
    "tictactoe_round_win": ("Three in a row!", "That round is mine!"),
    "tictactoe_round_lose": (
        "No way, chat got three in a row!",
        "Okay, chat takes that one.",
    ),
    "tictactoe_draw": ("A draw!", "Nobody wins that one."),
    "rps_intro": ("Rock Paper Scissors! Ready, chat?", "Jack en poy time!"),
    "rps_round_win": ("{hand} wins!", "Hehe, got you!"),
    "rps_round_lose": ("Chat got me!", "Ugh, chat wins that round."),
    "rps_tie": ("A tie! Again!", "Same hand!"),
    "too_slow": (
        "Too slow, chat!",
        "Chat, you have to be faster than that!",
        "Time's up, chat!",
    ),
    "chat_wins": ("Chat wins! Okay, you're good.", "Chat beat us! Well played."),
    "game_draw": ("It's a tie overall!", "A draw! Rematch?"),
    "answer": ("{answer}!", "Is it {answer}?"),
    "correct": ("Yes!",),
    "wrong": ("Oh no.",),
    "win": ("I win!",),
    "lose": ("Good game.",),
    "viewer_first": ("Chat got it first!",),
    "bored": ("I'm getting a little tired of this game.",),
}


def game_line(
    profile: Optional[CharacterProfile],
    kind: str,
    values: dict[str, Any],
    rng: random.Random,
) -> Optional[str]:
    pool = (profile.game_lines.get(kind) if profile else None) or _DEFAULT_LINES.get(
        kind
    )
    if not pool:
        return None
    template = rng.choice(list(pool))
    answer = str(values.get("answer", ""))[:60]
    if "{answer}" in template and not answer:
        return None
    for key, value in values.items():
        if key == "answer" or not isinstance(key, str) or not key.isidentifier():
            continue
        placeholder = "{" + key + "}"
        if placeholder in template:
            shown = str(value)[:40]
            if not shown:
                return None
            template = template.replace(placeholder, shown)
            if template.startswith(shown):
                template = shown[:1].upper() + template[1:]
    if "{" in template and "}" in template and "{answer}" not in template:
        return None  # a placeholder we could not fill
    if not template.startswith("{answer}") and answer.startswith(("The ", "A ", "An ")):
        answer = (
            answer[0].lower() + answer[1:]
        )  # "It's the Amazon", not "It's The Amazon"
    return template.replace("{answer}", answer)


def _score_line(scores: dict[str, int], names: dict[str, str]) -> str:
    parts = [
        f"{names.get(pid, 'Chat' if pid == 'viewers' else pid)} {score}"
        for pid, score in scores.items()
    ]
    return ", ".join(parts)


def command_reply(
    key: str, values: dict[str, Any], names: dict[str, str]
) -> Optional[str]:
    """Spoken reply for an engine Outcome. None means say nothing."""
    game = str(values.get("game") or "the game")
    summary = str(values.get("summary") or "")
    if key == "games_list":
        if values.get("count") == 1:
            return f"{summary} Want to play? Just say yes!"
        return f"{summary} Which one? Just say its name!" if summary else None
    if key == "how_to_play":
        if values.get("playing"):
            return (
                str(values.get("quick_rules") or values.get("rules") or "").strip()
                or None
            )
        rules = str(values.get("rules") or "").strip()
        return f"{rules} Ready? Say let's start!".strip()
    if key == "unsupported_game":
        name = str(values.get("name") or "That game").strip()
        name = name[:1].upper() + name[1:]
        return f"{name} isn't available yet. {summary}".strip()
    if key == "game_started":
        return None  # the game's intro line covers it
    if key == "already_playing":
        return f"We're already playing {game}!"
    if key == "no_game":
        return "We're not playing a game right now. Say let's play a game to start one!"
    if key == "game_stopped":
        scores = values.get("scores") or {}
        if values.get("reason") == "switch":
            return None
        return (
            f"Okay, game over! Final score: {_score_line(scores, names)}."
            if scores
            else "Okay, game over!"
        )
    if key == "no_other_games":
        return f"We only have one game right now. {summary}".strip()
    if key == "difficulty_set":
        return f"Okay, {values.get('level', 'new')} questions from now on."
    if key == "category_set":
        return f"Okay, {values.get('category', 'those')} questions next!"
    if key == "category_cleared":
        return "Okay, questions from every category!"
    if key == "unknown_category":
        categories = ", ".join(values.get("categories") or [])
        return (
            f"We don't have that category. We have {categories}."
            if categories
            else None
        )
    if key == "first_player_set":
        return f"Okay, {names.get(values.get('player', ''), 'they')} answers first next round!"
    if key == "round_skipped":
        return "Skipping to the next one!"
    if key == "cannot_skip":
        return None
    return None
