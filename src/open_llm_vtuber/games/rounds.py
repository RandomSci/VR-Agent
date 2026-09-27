"""Shared plumbing for round-based games (Tic-Tac-Toe, Rock Paper Scissors).

Handles what every such game needs and nothing game-specific: players and
modes (chat plays one character, or the two characters play each other),
phase timing that freezes while paused, scores, "first to N" finishing,
and the finished screen. Subclasses implement the rounds.
"""

from __future__ import annotations

import random
from typing import Any, Optional

from . import base
from .base import VIEWERS, Game, GameInfo, PlayerSpec, StepResult


def num(
    config: dict[str, Any], key: str, default: float, low: float, high: float
) -> float:
    try:
        value = float(config.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def info_from_config(config: dict[str, Any], defaults: dict[str, Any]) -> GameInfo:
    modes = tuple(
        m
        for m in (config.get("modes") or defaults["modes"])
        if m in ("chat", "characters")
    ) or ("chat",)
    return GameInfo(
        id=str(config.get("id") or defaults["id"]),
        display_name=str(config.get("display_name") or defaults["display_name"]),
        description=str(config.get("description") or ""),
        players=int(config.get("players", 2)),
        viewer_participation=bool(config.get("viewer_participation", True)),
        enabled=bool(config.get("enabled", True)),
        aliases=tuple(str(a) for a in config.get("aliases") or []),
        how_to_play=" ".join(str(config.get("how_to_play") or "").split())[:400],
        quick_rules=" ".join(str(config.get("quick_rules") or "").split())[:200],
        hint=" ".join(str(config.get("board_hint") or "").split())[:80],
        renderer=defaults["renderer"],
        min_players=int(config.get("min_players", 1)),
        modes=modes,
    )


class RoundGame(Game):
    """Two sides per round. A side is a character id or ``VIEWERS``."""

    def __init__(
        self,
        info: GameInfo,
        config: dict[str, Any],
        rng: Optional[random.Random] = None,
        options: Optional[dict] = None,
    ):
        self.info = info
        self.config = config
        self.rng = rng or random.Random()
        self.options = dict(options or {})
        c = config
        self.first_to = int(num(c, "first_to", 2, 1, 10))
        self.max_rounds = int(num(c, "max_rounds", 3, 1, 15))
        self.intro_s = num(c, "intro_seconds", 3, 0.5, 20)
        self.reveal_s = num(c, "reveal_seconds", 3.5, 1, 30)
        self.between_s = num(c, "between_rounds_seconds", 2.5, 0.5, 30)
        self.finish_s = num(c, "finish_display_seconds", 8, 2, 60)
        self.players: list[PlayerSpec] = []
        self.mode = "chat"
        self.sides: list[str] = []  # two side ids in a fixed order for the scoreboard
        self.scores: dict[str, int] = {}
        self.draws = 0
        self.round_no = 0
        self.rounds_played = 0
        self.round_winner: Optional[str] = None
        self.winner: Optional[str] = None
        self.phase = "intro"
        self.phase_started = 0.0
        self.phase_ends = 0.0
        self.phase_total = 0.0
        self._finished = False
        self._done = False
        self._paused = False
        self._pause_started = 0.0
        self.pause_reason = ""
        self.started_at = 0.0

    # ------------------------------------------------------------------
    # players
    # ------------------------------------------------------------------
    def _choose_sides(self, players: list[PlayerSpec]) -> None:
        ids = [p.id for p in players]
        wanted = self.options.get("mode")
        if wanted == "characters" and len(ids) >= 2 and "characters" in self.info.modes:
            first = self.options.get("first")
            first = first if first in ids else ids[0]
            second = next(i for i in ids if i != first)
            self.mode = "characters"
            self.sides = [first, second]
            return
        if "chat" not in self.info.modes and len(ids) >= 2:
            self.mode = "characters"
            self.sides = ids[:2]
            return
        opponent = self.options.get("opponent")
        if opponent not in ids:
            opponent = self.rng.choice(ids)
        self.mode = "chat"
        self.sides = [VIEWERS, opponent]

    def _player(self, player_id: Optional[str]) -> Optional[PlayerSpec]:
        return next((p for p in self.players if p.id == player_id), None)

    def name(self, side: Optional[str]) -> str:
        if side == VIEWERS:
            return "Chat"
        spec = self._player(side)
        return spec.name if spec else ""

    def other(self, side: str) -> str:
        return self.sides[1] if side == self.sides[0] else self.sides[0]

    def characters(self) -> list[str]:
        return [s for s in self.sides if s != VIEWERS]

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def done(self) -> bool:
        return self._done

    @property
    def paused(self) -> bool:
        return self._paused

    def start(self, players: list[PlayerSpec], now: float) -> StepResult:
        if not players:
            raise ValueError(f"{self.info.display_name} needs at least one character")
        self.players = list(players)
        self._choose_sides(self.players)
        self.scores = {s: 0 for s in self.sides}
        self.started_at = now
        self.set_phase("intro", now, self.intro_s)
        result = StepResult()
        result.event(
            base.GAME_STARTED,
            game=self.info.id,
            players=self.characters(),
            mode=self.mode,
            rounds=self.max_rounds,
        )
        speaker = self.characters()[0] if self.mode == "characters" else self.sides[1]
        result.line(
            speaker, f"{self.info.id}_intro", opponent=self.name(self.other(speaker))
        )
        return result

    def stop(self, now: float, reason: str) -> StepResult:
        result = StepResult()
        if self._done:
            return result
        self._finished = True
        self._done = True
        result.event(
            base.GAME_STOPPED,
            game=self.info.id,
            reason=reason,
            scores=dict(self.scores),
        )
        return result

    def pause(self, now: float, reason: str) -> StepResult:
        result = StepResult()
        if self._paused or self._finished:
            return result
        self._paused = True
        self._pause_started = now
        self.pause_reason = reason
        result.event(base.GAME_PAUSED, game=self.info.id, reason=reason)
        return result

    def resume(self, now: float) -> StepResult:
        result = StepResult()
        if not self._paused:
            return result
        frozen = max(0.0, now - self._pause_started)
        self.phase_ends += frozen
        self.phase_started += frozen
        self.on_resume(frozen)
        self._paused = False
        self.pause_reason = ""
        result.event(base.GAME_RESUMED, game=self.info.id)
        return result

    def on_resume(self, frozen: float) -> None:
        """Subclasses shift their own deadlines by ``frozen`` seconds."""

    def character_done(self, player: str, now: float) -> StepResult:
        return StepResult()  # game lines never block these games

    def set_phase(self, phase: str, now: float, seconds: float) -> None:
        self.phase = phase
        self.phase_started = now
        self.phase_total = seconds
        self.phase_ends = now + seconds

    def remaining(self, now: float) -> float:
        return max(
            0.0, self.phase_ends - (self._pause_started if self._paused else now)
        )

    # ------------------------------------------------------------------
    # rounds
    # ------------------------------------------------------------------
    def score_round(self, winner: Optional[str], now: float, **data: Any) -> StepResult:
        """Record a round result, then show it."""
        result = StepResult()
        self.round_winner = winner
        self.rounds_played += 1
        if winner is None:
            self.draws += 1
        else:
            self.scores[winner] = self.scores.get(winner, 0) + 1
            result.event(base.ANSWER_CORRECT, player=winner, round=self.round_no)
            result.event(base.SCORE_CHANGED, scores=dict(self.scores))
        result.event(base.ROUND_FINISHED, round=self.round_no, winner=winner, **data)
        self.set_phase("reveal", now, self.reveal_s)
        return result

    def game_over(self) -> bool:
        if any(score >= self.first_to for score in self.scores.values()):
            return True
        return self.round_no >= self.max_rounds

    def finish(self, now: float) -> StepResult:
        result = StepResult()
        self._finished = True
        top = max(self.scores.values()) if self.scores else 0
        leaders = [s for s, score in self.scores.items() if score == top]
        self.winner = leaders[0] if len(leaders) == 1 and top > 0 else None
        loser = self.other(self.winner) if self.winner else None
        self.set_phase("finished", now, self.finish_s)
        result.event(
            base.GAME_FINISHED,
            game=self.info.id,
            winner=self.winner,
            loser=loser if loser != VIEWERS else None,
            scores=dict(self.scores),
        )
        if self.winner and self.winner != VIEWERS:
            result.line(self.winner, "win")
            if loser and loser != VIEWERS:
                result.line(loser, "lose")
        elif self.winner == VIEWERS:
            result.line(self.sides[1], "chat_wins")
        else:
            result.line(self.characters()[0], "game_draw")
        return result

    def tick_common(self, now: float) -> tuple[bool, StepResult]:
        """Handles intro, reveal, between and finished. (handled, result)"""
        result = StepResult()
        if self.phase == "intro":
            result.extend(self.start_round(now))
            return True, result
        if self.phase == "reveal":
            if self.game_over():
                result.extend(self.finish(now))
            else:
                self.set_phase("between", now, self.between_s)
            return True, result
        if self.phase == "between":
            result.extend(self.start_round(now))
            return True, result
        if self.phase == "finished":
            self._done = True
            return True, result
        return False, result

    def start_round(self, now: float) -> StepResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def at_checkpoint(self) -> bool:
        return self._paused or self.phase in ("intro", "reveal", "between", "finished")

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------
    def score_chips(self) -> list[dict[str, Any]]:
        return [
            {"id": s, "name": self.name(s), "score": self.scores.get(s, 0)}
            for s in self.sides
        ]

    def final_status(self) -> str:
        if self.winner == VIEWERS:
            return "Chat wins!"
        if self.winner:
            return f"{self.name(self.winner)} wins!"
        return "It's a tie!"

    def frame(
        self, now: float, status: str, timed: bool, highlight: Optional[str]
    ) -> dict[str, Any]:
        return {
            "renderer": self.info.renderer,
            "game_id": self.info.id,
            "title": self.info.display_name.upper(),
            "round": self.round_no,
            "rounds": self.max_rounds,
            "phase": self.phase,
            "mode": self.mode,
            "status": "Paused" if self._paused else status,
            "paused": self._paused,
            "finished": self._finished,
            "winner": self.winner,
            "highlight": self.winner if self.phase == "finished" else highlight,
            "hint": self.info.hint if self.mode == "chat" else "",
            "turn_label": "CHAT'S TURN"
            if self.phase == "vote" and not self._paused
            else "",
            "timer": {
                "remaining_ms": int(self.remaining(now) * 1000),
                "total_ms": int(self.phase_total * 1000),
            }
            if timed and not self._paused
            else None,
            "scores": self.score_chips(),
        }

    def base_state(self) -> dict[str, Any]:
        return {
            "game": self.info.id,
            "mode": self.mode,
            "sides": list(self.sides),
            "phase": self.phase,
            "round": self.round_no,
            "rounds": self.max_rounds,
            "first_to": self.first_to,
            "scores": dict(self.scores),
            "draws": self.draws,
            "round_winner": self.round_winner,
            "winner": self.winner,
            "paused": self._paused,
            "pause_reason": self.pause_reason,
            "finished": self._finished,
            "done": self._done,
            "rounds_played": self.rounds_played,
        }
