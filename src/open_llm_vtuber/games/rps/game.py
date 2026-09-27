"""Rock Paper Scissors (jack en poy) on the in-room Game Board.

Chat mode: each round chat votes rock, paper or scissors during a countdown
(words, emoji or the Filipino bato, papel, gunting); the most voted hand is
chat's throw. The character's hand is drawn at random when the round opens
and only revealed afterwards, so it can never react to chat's votes.
Character mode: the two characters throw at each other and chat watches.
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

HANDS = ("rock", "paper", "scissors")
BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
HAND_WORDS = {
    "rock": "rock",
    "rocks": "rock",
    "stone": "rock",
    "bato": "rock",
    "r": "rock",
    "✊": "rock",
    "\U0001faa8": "rock",
    "\U0001f44a": "rock",
    "paper": "paper",
    "papel": "paper",
    "p": "paper",
    "✋": "paper",
    "\U0001f590": "paper",
    "\U0001f4c4": "paper",
    "\U0001f91a": "paper",
    "scissors": "scissors",
    "scissor": "scissors",
    "gunting": "scissors",
    "s": "scissors",
    "✌": "scissors",
    "✂": "scissors",
}


_HAND_FILLER = frozenset(
    "i ill i'll choose chose pick go going with throw play vote my our is it "
    "a the for please pls plz ok okay then this time final answer we".split()
)


def parse_hand(text: str) -> Optional[str]:
    """rock, paper or scissors from chat text, or None.

    Accepts "rock", "✊", "I choose paper", "going with scissors please".
    A message with one hand word plus only filler words counts; anything
    longer or mixed ("rock is overrated tbh") stays ordinary chat.
    """
    raw = str(text or "").strip().lower()
    raw = raw.replace("\ufe0f", "")
    if not raw or len(raw) > 40:
        return None
    if raw in HAND_WORDS:
        return HAND_WORDS[raw]
    for symbol, hand in HAND_WORDS.items():
        if (
            len(symbol) == 1
            and not symbol.isalpha()
            and symbol in raw
            and len(raw) <= 3
        ):
            return hand
    words = re.findall(r"[a-z']+", raw)
    if not 1 <= len(words) <= 6:
        return None
    hands = {HAND_WORDS[w] for w in words if w in HAND_WORDS and len(w) > 1}
    rest = [w for w in words if w not in HAND_WORDS and w not in _HAND_FILLER]
    if len(hands) == 1 and not rest:
        return hands.pop()
    return None


def judge(a: str, b: str) -> int:
    """1 when a wins, -1 when b wins, 0 for a tie."""
    if a == b:
        return 0
    return 1 if BEATS[a] == b else -1


class RockPaperScissors(RoundGame):
    def __init__(self, info, config, rng=None, options=None):
        super().__init__(info, config, rng, options)
        c = config
        self.first_to = int(num(c, "first_to", 3, 1, 10))
        self.max_rounds = int(num(c, "max_rounds", 7, 1, 15))
        self.vote_s = num(c, "vote_seconds", 10, 3, 60)
        self.quiet_close_s = num(c, "close_after_quiet_seconds", 3.0, 1, 20)
        self.countdown_s = num(c, "character_countdown_seconds", 2.4, 0.5, 10)
        self.vote = ChatVote(feed_size=int(num(c, "viewer_feed_size", 4, 1, 10)))
        self.throws: dict[str, Optional[str]] = {}
        self.hidden: dict[str, str] = {}  # characters' hands, drawn before chat votes
        self.last_vote_at = 0.0
        self.timed_out = False
        self.history: list[dict[str, Any]] = []

    def on_resume(self, frozen: float) -> None:
        if self.last_vote_at:
            self.last_vote_at += frozen

    def start_round(self, now: float) -> StepResult:
        result = StepResult()
        self.round_no += 1
        self.round_winner = None
        self.timed_out = False
        self.throws = {s: None for s in self.sides}
        self.hidden = {s: self.rng.choice(HANDS) for s in self.characters()}
        result.event(base.ROUND_STARTED, round=self.round_no, rounds=self.max_rounds)
        if self.mode == "chat":
            self.vote.start()
            self.last_vote_at = 0.0
            self.set_phase("vote", now, self.vote_s)
            result.event(base.QUESTION_SHOWN, round=self.round_no, game=self.info.id)
        else:
            self.set_phase("countdown", now, self.countdown_s)
            for side in self.sides:
                result.event(base.CHARACTER_TURN, player=side, round=self.round_no)
        return result

    def tick(self, now: float) -> StepResult:
        result = StepResult()
        if self._paused or self._done:
            return result
        if self.phase == "vote":
            quiet = self.last_vote_at and now - self.last_vote_at >= self.quiet_close_s
            if now >= self.phase_ends or quiet:
                result.extend(self._reveal_round(now))
            return result
        if now < self.phase_ends:
            return result
        if self.phase == "countdown":
            return result.extend(self._reveal_round(now))
        _handled, common = self.tick_common(now)
        return result.extend(common)

    def _reveal_round(self, now: float) -> StepResult:
        result = StepResult()
        for side, hand in self.hidden.items():
            self.throws[side] = hand
        if self.mode == "chat":
            chat_hand = self.vote.close()
            character = self.sides[1]
            if chat_hand is None:
                # Nobody threw in time: the round goes to the character.
                self.timed_out = True
                result.event(
                    base.VIEWER_TIMEOUT, game=self.info.id, round=self.round_no
                )
                result.line(character, "too_slow")
                self.history.append(
                    {
                        "round": self.round_no,
                        "chat": None,
                        character: self.throws[character],
                    }
                )
                result.extend(self.score_round(character, now, timeout=True))
                return result
            self.throws[VIEWERS] = chat_hand
        a, b = self.sides
        for side in self.sides:
            result.event(
                base.MOVE_MADE, player=side, move=self.throws[side], game=self.info.id
            )
        outcome = judge(self.throws[a], self.throws[b])
        winner = a if outcome == 1 else b if outcome == -1 else None
        self.history.append(
            {"round": self.round_no, **{s: self.throws[s] for s in self.sides}}
        )
        result.extend(self.score_round(winner, now, hands=dict(self.throws)))
        if winner is None:
            result.line(self.characters()[0], "rps_tie")
        elif winner == VIEWERS:
            result.line(self.sides[1], "rps_round_lose")
        else:
            result.line(winner, "rps_round_win", hand=self.throws[winner] or "")
        return result

    def handle_viewer_message(
        self, username: str, text: str, now: float
    ) -> tuple[bool, StepResult]:
        result = StepResult()
        if self._done or self.mode != "chat":
            return False, result
        hand = parse_hand(text)
        if hand is None:
            return False, result
        if self.phase != "vote" or self._paused:
            self.vote.cast(username, hand, hand)  # recorded as too late
            return True, result
        self.vote.cast(username, hand, hand)
        self.last_vote_at = now
        result.event(
            base.VIEWER_ANSWERED,
            username=username[:40],
            correct=True,
            round=self.round_no,
        )
        return True, result

    def _status(self) -> str:
        if self.phase == "intro":
            if self.mode == "chat":
                return (
                    f"Chat vs {self.name(self.sides[1])}! Type rock, paper or scissors"
                )
            return f"{self.name(self.sides[0])} vs {self.name(self.sides[1])}!"
        if self.phase == "vote":
            return "Chat's turn! Rock, paper or scissors?"
        if self.phase == "countdown":
            return "Rock... paper... scissors..."
        if self.phase == "reveal":
            if self.timed_out:
                return "Too slow, chat!"
            if self.round_winner:
                return f"{self.name(self.round_winner)} wins the round!"
            return "Tie!"
        if self.phase == "between":
            return "Next round..."
        return self.final_status()

    def view(self, now: float) -> dict[str, Any]:
        revealed = self.phase in ("reveal", "between", "finished")
        frame = self.frame(
            now,
            self._status(),
            timed=self.phase in ("vote", "countdown"),
            highlight=self.round_winner if revealed else None,
        )
        counts = self.vote.counts() if self.mode == "chat" else {}
        frame["body"] = {
            "hands": [
                {
                    "id": side,
                    "name": self.name(side),
                    "hand": self.throws.get(side) if revealed else None,
                    "winner": revealed and self.round_winner == side,
                }
                for side in self.sides
            ],
            "votes": {h: counts.get(h, 0) for h in HANDS}
            if self.mode == "chat"
            else None,
            "voting": self.phase == "vote",
            "timed_out": self.timed_out,
            "first_to": self.first_to,
            "hint": "Type rock, paper or scissors" if self.mode == "chat" else "",
            "viewer_feed": list(self.vote.feed) if self.mode == "chat" else [],
        }
        return frame

    def state(self) -> dict[str, Any]:
        out = self.base_state()
        out.update(
            {
                "throws": dict(self.throws),
                "votes": self.vote.counts(),
                "late_votes": self.vote.late,
                "history": list(self.history),
            }
        )
        return out


def load(config: dict[str, Any], game_dir: Path) -> GameFactory:
    info = info_from_config(
        config,
        {
            "id": "rps",
            "display_name": "Rock Paper Scissors",
            "renderer": "rps",
            "modes": ("chat", "characters"),
        },
    )

    def create(rng=None, options=None) -> RockPaperScissors:
        return RockPaperScissors(info, config, rng=rng, options=options)

    return GameFactory(info=info, create=create, config=dict(config))
