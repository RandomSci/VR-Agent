"""Tic-Tac-Toe on the in-room Game Board.

Chat mode: chat (X) plays one character (O). On chat's turn viewers type a
cell number 1 to 9; the most voted free cell is played when the vote closes.
Character mode: the two characters play each other and chat watches.
Characters pick moves with a small rule-based player (win, block, center,
corner) whose accuracy comes from their game skill. No LLM anywhere.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from .. import base
from ..base import VIEWERS, StepResult
from ..registry import GameFactory
from ..rounds import RoundGame, info_from_config, num
from ..voting import ChatVote

LINES = (
    (0, 1, 2),
    (3, 4, 5),
    (6, 7, 8),
    (0, 3, 6),
    (1, 4, 7),
    (2, 5, 8),
    (0, 4, 8),
    (2, 4, 6),
)
CELL_WORDS = {
    "top left": 0,
    "top middle": 1,
    "top center": 1,
    "top": 1,
    "top right": 2,
    "middle left": 3,
    "left": 3,
    "center": 4,
    "centre": 4,
    "middle": 4,
    "middle right": 5,
    "right": 5,
    "bottom left": 6,
    "bottom middle": 7,
    "bottom center": 7,
    "bottom": 7,
    "bottom right": 8,
    "upper left": 0,
    "upper right": 2,
    "lower left": 6,
    "lower right": 8,
    "center middle": 4,
    "middle center": 4,
    "middle middle": 4,
}
_CELL_RE = re.compile(r"^\s*(?:#|cell\s*|box\s*|square\s*)?([1-9])\s*[!.]*\s*$", re.I)
_NUMBER_WORDS = {
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}
# Words that may surround a move without changing it: "put X in 5",
# "I choose 5", "X on the middle square please". Anything else left over
# means the message is ordinary chat, not a move.
_FILLER = frozenset(
    "i ill i'll choose chose pick picking go going put place placing play mark "
    "take want vote votes voting my our move x o in on at into the a to for "
    "number no num cell box square spot tile position one please pls plz "
    "lets let's let us is it its it's this that ok okay then now yes".split()
)
_SIDE_WORDS = frozenset(
    "top bottom left right middle center centre upper lower corner".split()
)


def parse_cell(text: str) -> Optional[int]:
    """0-based cell index from chat text, or None when it is not a move.

    Accepts "5", "#5", "cell 5", "five", "center", "middle square",
    "top left corner", "put X in 5", "I choose 5", "5 please".
    """
    raw = str(text or "").strip().lower()
    if len(raw) > 40:
        return None
    match = _CELL_RE.match(raw)
    if match:
        return int(match.group(1)) - 1
    words = re.findall(r"[a-z']+|[0-9]+", raw.replace("#", " "))
    words = [_NUMBER_WORDS.get(w, w) for w in words]
    digits = [w for w in words if w.isdigit()]
    rest = [w for w in words if not w.isdigit() and w not in _FILLER]
    if digits:
        if len(digits) == 1 and not rest and len(digits[0]) == 1 and digits[0] != "0":
            return int(digits[0]) - 1
        return None
    if not rest or any(w not in _SIDE_WORDS for w in rest):
        return None
    rest = [w for w in rest if w != "corner"]
    phrase = " ".join(rest)
    if phrase in CELL_WORDS:
        return CELL_WORDS[phrase]
    # "left top" means "top left"
    if len(rest) == 2 and f"{rest[1]} {rest[0]}" in CELL_WORDS:
        return CELL_WORDS[f"{rest[1]} {rest[0]}"]
    return None


def winning_line(cells: list[str]) -> Optional[tuple[int, int, int]]:
    for a, b, c in LINES:
        if cells[a] and cells[a] == cells[b] == cells[c]:
            return (a, b, c)
    return None


def best_move(cells: list[str], mark: str, skill: float, rng) -> int:
    free = [i for i, v in enumerate(cells) if not v]
    if not free:
        return -1
    if rng.random() > skill:
        return rng.choice(free)
    enemy = "O" if mark == "X" else "X"
    for who in (mark, enemy):  # win if possible, otherwise block
        for a, b, c in LINES:
            trio = [cells[a], cells[b], cells[c]]
            if trio.count(who) == 2 and trio.count("") == 1:
                return (a, b, c)[trio.index("")]
    if 4 in free:
        return 4
    corners = [i for i in (0, 2, 6, 8) if i in free]
    return rng.choice(corners or free)


class TicTacToe(RoundGame):
    def __init__(self, info, config, rng=None, options=None):
        super().__init__(info, config, rng, options)
        c = config
        self.vote_s = num(c, "vote_seconds", 12, 3, 60)
        self.quiet_close_s = num(c, "close_after_quiet_seconds", 3.5, 1, 20)
        self.think_s = num(c, "character_think_seconds", 1.6, 0.3, 10)
        self.vote = ChatVote(feed_size=int(num(c, "viewer_feed_size", 4, 1, 10)))
        self.cells: list[str] = [""] * 9
        self.marks: dict[str, str] = {}  # side -> "X" / "O"
        self.turn: Optional[str] = None  # side to move
        self.starter: Optional[str] = None
        self.win_cells: Optional[tuple[int, int, int]] = None
        self.last_move: Optional[int] = None
        self.last_vote_at = 0.0
        self.timed_out = False

    def on_resume(self, frozen: float) -> None:
        if self.last_vote_at:
            self.last_vote_at += frozen

    # ------------------------------------------------------------------
    def start_round(self, now: float) -> StepResult:
        result = StepResult()
        self.round_no += 1
        self.cells = [""] * 9
        self.win_cells = None
        self.last_move = None
        self.round_winner = None
        self.marks = {self.sides[0]: "X", self.sides[1]: "O"}
        # The starter alternates so nobody always has the first move.
        self.starter = self.sides[(self.round_no - 1) % 2]
        result.event(base.ROUND_STARTED, round=self.round_no, rounds=self.max_rounds)
        result.extend(self._begin_turn(self.starter, now))
        return result

    def _begin_turn(self, side: str, now: float) -> StepResult:
        result = StepResult()
        self.turn = side
        self.timed_out = False
        if side == VIEWERS:
            self.vote.start()
            self.last_vote_at = 0.0
            self.set_phase("vote", now, self.vote_s)
            result.event(base.QUESTION_SHOWN, round=self.round_no, game=self.info.id)
        else:
            self.set_phase("think", now, self.think_s)
            result.event(base.CHARACTER_TURN, player=side, round=self.round_no)
        return result

    def _place(self, side: str, cell: int, now: float) -> StepResult:
        result = StepResult()
        self.cells[cell] = self.marks[side]
        self.last_move = cell
        result.event(base.MOVE_MADE, player=side, move=cell + 1, game=self.info.id)
        line = winning_line(self.cells)
        if line:
            self.win_cells = line
            result.extend(self.score_round(side, now, line=[i + 1 for i in line]))
            if side == VIEWERS:
                result.line(self.other(side), "tictactoe_round_lose")
            else:
                result.line(side, "tictactoe_round_win")
            return result
        if all(self.cells):
            result.extend(self.score_round(None, now))
            result.line(self.characters()[0], "tictactoe_draw")
            return result
        result.extend(self._begin_turn(self.other(side), now))
        return result

    def tick(self, now: float) -> StepResult:
        result = StepResult()
        if self._paused or self._done:
            return result
        if self.phase == "vote":
            quiet = self.last_vote_at and now - self.last_vote_at >= self.quiet_close_s
            if now >= self.phase_ends or quiet:
                result.extend(self._close_vote(now))
            return result
        if now < self.phase_ends:
            return result
        if self.phase == "think":
            side = self.turn or self.sides[1]
            spec = self._player(side)
            skill = (spec.skill if spec else {}).get("medium", 0.6)
            move = best_move(self.cells, self.marks[side], skill, self.rng)
            result.extend(self._place(side, move, now))
            return result
        _handled, common = self.tick_common(now)
        return result.extend(common)

    def _close_vote(self, now: float) -> StepResult:
        result = StepResult()
        choice = self.vote.close()
        free = [i for i, v in enumerate(self.cells) if not v]
        if choice is None or int(choice) not in free:
            # Nobody voted in time: one of the girls teases chat and a random
            # free cell is played so the game keeps moving.
            self.timed_out = True
            result.event(base.VIEWER_TIMEOUT, game=self.info.id, round=self.round_no)
            result.line(self.sides[1], "too_slow")
            choice = self.rng.choice(free)
        result.extend(self._place(VIEWERS, int(choice), now))
        return result

    # ------------------------------------------------------------------
    def handle_viewer_message(
        self, username: str, text: str, now: float
    ) -> tuple[bool, StepResult]:
        result = StepResult()
        if self._done or self.mode != "chat":
            return False, result
        cell = parse_cell(text)
        if cell is None:
            return False, result
        shown = str(cell + 1)
        if self.phase != "vote" or self._paused:
            self.vote.cast(username, str(cell), shown)  # recorded as too late
            return True, result
        if self.cells[cell]:
            return True, result  # taken cell: swallow, it cannot win the vote
        self.vote.cast(username, str(cell), shown)
        self.last_vote_at = now
        result.event(
            base.VIEWER_ANSWERED,
            username=username[:40],
            correct=True,
            round=self.round_no,
        )
        return True, result

    # ------------------------------------------------------------------
    def _status(self) -> str:
        if self.phase == "intro":
            if self.mode == "chat":
                return f"Chat vs {self.name(self.sides[1])}! Type 1-9 to vote"
            return f"{self.name(self.sides[0])} vs {self.name(self.sides[1])}!"
        if self.phase == "vote":
            return "Chat's turn! Type a number 1-9"
        if self.phase == "think":
            return f"{self.name(self.turn)} is thinking..."
        if self.phase == "reveal":
            if self.round_winner:
                return f"{self.name(self.round_winner)} wins the round!"
            return "Draw!"
        if self.phase == "between":
            return "Next round..."
        return self.final_status()

    def view(self, now: float) -> dict[str, Any]:
        counts = self.vote.counts() if self.phase == "vote" else {}
        frame = self.frame(
            now,
            self._status(),
            timed=self.phase == "vote",
            highlight=self.turn
            if self.phase in ("vote", "think")
            else self.round_winner,
        )
        frame["body"] = {
            "cells": [
                {"mark": mark, "votes": counts.get(str(i), 0)}
                for i, mark in enumerate(self.cells)
            ],
            "sides": {
                "X": {"id": self.sides[0], "name": self.name(self.sides[0])}
                if self.sides
                else None,
                "O": {"id": self.sides[1], "name": self.name(self.sides[1])}
                if self.sides
                else None,
            },
            "turn": self.marks.get(self.turn or "", None)
            if self.phase in ("vote", "think")
            else None,
            "line": [i for i in self.win_cells] if self.win_cells else None,
            "last_move": self.last_move,
            "draws": self.draws,
            "votes": self.vote.total if self.phase == "vote" else 0,
            "timed_out": self.timed_out,
            "hint": "Type 1-9" if self.mode == "chat" else "",
            "viewer_feed": list(self.vote.feed) if self.mode == "chat" else [],
        }
        return frame

    def state(self) -> dict[str, Any]:
        out = self.base_state()
        out.update(
            {
                "board": "".join(c or "." for c in self.cells),
                "turn": self.turn,
                "marks": dict(self.marks),
                "votes": self.vote.counts(),
                "late_votes": self.vote.late,
            }
        )
        return out


def load(config: dict[str, Any], game_dir: Path) -> GameFactory:
    info = info_from_config(
        config,
        {
            "id": "tictactoe",
            "display_name": "Tic-Tac-Toe",
            "renderer": "grid",
            "modes": ("chat", "characters"),
        },
    )

    def create(rng=None, options=None) -> TicTacToe:
        return TicTacToe(info, config, rng=rng, options=options)

    return GameFactory(info=info, create=create, config=dict(config))
