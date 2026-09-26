"""Chat votes for games where chat plays as one team.

One vote per viewer per round (a viewer may change it), the most voted option
wins, ties go to the option that reached the top count first. Votes after the
round closed never change it; they are recorded as "too late" so the board
can say so.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Optional


class ChatVote:
    def __init__(self, feed_size: int = 4, max_voters: int = 500):
        self.votes: dict[str, str] = {}
        self.order: list[str] = []  # options in the order they were first voted
        self.open = False
        self.feed: Deque[dict] = deque(maxlen=feed_size)
        self.max_voters = max_voters
        self.late: int = 0

    def start(self) -> None:
        self.votes.clear()
        self.order.clear()
        self.feed.clear()
        self.open = True

    def close(self) -> Optional[str]:
        self.open = False
        return self.winner()

    def cast(self, user: str, option: str, shown: str = "") -> bool:
        """True when the vote counted; False when voting is closed (too late)."""
        user = (user or "viewer").strip()[:40] or "viewer"
        if not self.open:
            self.late += 1
            self.feed.append({"user": user, "text": shown or option, "mark": "late"})
            return False
        if user not in self.votes and len(self.votes) >= self.max_voters:
            return True  # enough voters; ignore the rest quietly
        self.votes[user] = option
        if option not in self.order:
            self.order.append(option)
        self.feed.append({"user": user, "text": shown or option, "mark": "vote"})
        return True

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for option in self.votes.values():
            out[option] = out.get(option, 0) + 1
        return out

    def winner(self) -> Optional[str]:
        counts = self.counts()
        if not counts:
            return None
        top = max(counts.values())
        return next(o for o in self.order if counts.get(o) == top)

    @property
    def total(self) -> int:
        return len(self.votes)
