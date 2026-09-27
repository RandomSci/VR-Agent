"""Trivia Battle: characters and chat race to answer local trivia questions.

Everything here is deterministic given the random source: which question is
asked, whether a character answers correctly (per-character skill by
difficulty), which wrong answer it gives, viewer answer matching, scores,
timers and the winner. The room only renders the view model, plays sounds
and speaks the requested template lines.
"""

from __future__ import annotations

import json
import os
import random
from collections import deque
from pathlib import Path
from typing import Any, Deque, Optional

from .. import base
from ..base import (
    VIEWERS,
    Game,
    GameInfo,
    PlayerSpec,
    SetCategory,
    SetDifficulty,
    SetFirstPlayer,
    NextRound,
    StepResult,
)
from ..matching import is_correct, looks_like_answer, normalise
from ..registry import GameFactory
from .bank import QuestionBank

DIFFICULTIES = ("easy", "medium", "hard")
PHASES = ("intro", "question", "turn", "reveal", "between", "finished")


def _num(
    config: dict[str, Any], key: str, default: float, low: float, high: float
) -> float:
    try:
        value = float(config.get(key, default))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def load_questions(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    questions = []
    for raw in data.get("questions", []):
        question = str(raw.get("question") or "").strip()
        correct = str(raw.get("correct_answer") or "").strip()
        accepted = [str(a) for a in raw.get("accepted_answers") or [] if str(a).strip()]
        wrong = [str(w) for w in raw.get("wrong_answers") or [] if str(w).strip()]
        difficulty = str(raw.get("difficulty") or "medium").lower()
        category = str(raw.get("category") or "general").lower()
        if (
            not question
            or not correct
            or len(wrong) < 1
            or difficulty not in DIFFICULTIES
        ):
            continue
        if correct not in accepted:
            accepted.insert(0, correct)
        questions.append(
            {
                "id": str(raw.get("id") or f"q{len(questions)}"),
                "question": question[:200],
                "correct_answer": correct[:60],
                "accepted_answers": accepted,
                "wrong_answers": wrong,
                "difficulty": difficulty,
                "category": category,
            }
        )
    if len(questions) < 5:
        raise ValueError("question bank has fewer than 5 valid questions")
    return questions


def load(config: dict[str, Any], game_dir: Path) -> GameFactory:
    questions = load_questions(game_dir / "questions.json")
    categories = tuple(sorted({q["category"] for q in questions}))
    ai = (
        config.get("ai_questions")
        if isinstance(config.get("ai_questions"), dict)
        else {}
    )
    cache_dir = Path(
        str(config.get("cache_dir") or os.environ.get("VR_AGENT_CACHE_DIR") or "cache")
    )
    bank = QuestionBank(
        questions,
        cache_dir=cache_dir,
        max_ai_questions=int(_num(ai, "max_saved", 1500, 0, 20000)),
    )
    info = GameInfo(
        id=str(config.get("id") or "trivia"),
        display_name=str(config.get("display_name") or "Trivia Battle"),
        description=str(config.get("description") or ""),
        players=int(config.get("players", 2)),
        viewer_participation=bool(config.get("viewer_participation", True)),
        enabled=bool(config.get("enabled", True)),
        categories=categories,
        difficulties=DIFFICULTIES,
        aliases=tuple(str(a) for a in config.get("aliases") or []),
        how_to_play=" ".join(str(config.get("how_to_play") or "").split())[:400],
        quick_rules=" ".join(str(config.get("quick_rules") or "").split())[:200],
        hint=" ".join(str(config.get("board_hint") or "").split())[:80],
        renderer="trivia",
        min_players=1,
        modes=("chat", "characters"),
    )

    def create(
        rng: Optional[random.Random] = None, options: Optional[dict] = None
    ) -> "TriviaBattle":
        return TriviaBattle(info, config, bank, rng=rng, options=options)

    factory = GameFactory(info=info, create=create, config=dict(config))
    factory.bank = bank  # type: ignore[attr-defined]  # the room's question maker adds to it
    return factory


class TriviaBattle(Game):
    def __init__(
        self,
        info: GameInfo,
        config: dict[str, Any],
        questions: list[dict[str, Any]],
        rng: Optional[random.Random] = None,
        options: Optional[dict] = None,
    ):
        self.info = info
        self.bank = (
            questions
            if isinstance(questions, QuestionBank)
            else QuestionBank(list(questions), cache_dir=None)
        )
        self.rng = rng or random.Random()
        options = options or {}
        c = config
        self.rounds_total = int(_num(c, "rounds_per_game", 5, 1, 20))
        self.intro_s = _num(c, "intro_seconds", 3, 0.5, 20)
        # Chat's exclusive turn. YouTube chat takes a few seconds to travel
        # (typing, YouTube, the page, Playwright), so this must stay generous.
        self.viewer_window_s = _num(
            c,
            "viewer_answer_window_seconds",
            _num(c, "viewer_window_seconds", 12, 2, 60),
            2,
            60,
        )
        self.config_keep_window = bool(c.get("wrong_answers_keep_window", True))
        self.question_shown_at = 0.0
        self.chat_played = False
        self.chat_timing: Deque[dict[str, Any]] = deque(maxlen=50)
        self.think_s = _num(c, "character_think_seconds", 2.5, 0.5, 20)
        self.line_timeout_s = _num(c, "line_timeout_seconds", 12, 2, 60)
        self.reveal_s = _num(c, "reveal_seconds", 4.5, 1, 30)
        self.between_s = _num(c, "between_rounds_seconds", 2.5, 0.5, 30)
        self.finish_s = _num(c, "finish_display_seconds", 9, 2, 60)
        self.max_answer_words = int(_num(c, "max_answer_words", 5, 1, 12))
        self.typo_tolerance = _num(c, "typo_tolerance", 0.86, 0.6, 1.0)
        feed = int(_num(c, "viewer_feed_size", 4, 1, 10))

        difficulty = str(
            options.get("difficulty") or c.get("default_difficulty") or "medium"
        )
        self.difficulty = (
            difficulty if difficulty in (*DIFFICULTIES, "mixed") else "medium"
        )
        category = options.get("category")
        self.category: Optional[str] = category if category in info.categories else None

        self.players: list[PlayerSpec] = []
        self.scores: dict[str, int] = {}
        self.round_no = 0
        self.phase = "intro"
        self.phase_started = 0.0
        self.phase_ends = 0.0
        self.phase_total = 0.0
        self.question: Optional[dict[str, Any]] = None
        self.last_question: Optional[dict[str, Any]] = None
        self.used: set[str] = set()
        self.order: list[str] = []
        self.turn_index = 0
        self.awaiting_line: Optional[str] = None
        self.line_deadline = 0.0
        self.pending_correct: Optional[bool] = None
        self.answers: list[dict[str, Any]] = []
        self.round_winner: Optional[str] = None
        self.first_viewer: Optional[str] = None
        self.viewer_attempts: dict[str, int] = {}
        self.viewer_feed: Deque[dict[str, Any]] = deque(maxlen=feed)
        self.next_first: Optional[str] = None
        self.winner: Optional[str] = None
        self._finished = False
        self._done = False
        self._paused = False
        self._pause_started = 0.0
        self.pause_reason = ""
        self.rounds_played = 0
        self.started_at = 0.0

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def done(self) -> bool:
        """Finished and the result screen has been shown long enough."""
        return self._done

    @property
    def paused(self) -> bool:
        return self._paused

    def start(self, players: list[PlayerSpec], now: float) -> StepResult:
        if not players:
            raise ValueError("Trivia Battle needs at least one character")
        self.players = list(players)[: max(1, self.info.players)]
        self.scores = {p.id: 0 for p in self.players}
        if self.info.viewer_participation:
            self.scores[VIEWERS] = 0
        self.started_at = now
        self._set_phase("intro", now, self.intro_s)
        result = StepResult()
        result.event(
            base.GAME_STARTED,
            game=self.info.id,
            players=[p.id for p in self.players],
            rounds=self.rounds_total,
        )
        result.line(self.players[0].id, "intro")
        return result

    def stop(self, now: float, reason: str) -> StepResult:
        result = StepResult()
        if self._done:
            return result
        self._finished = True
        self._done = True
        self.awaiting_line = None
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
        if self.awaiting_line:
            self.line_deadline += frozen
        self._paused = False
        self.pause_reason = ""
        result.event(base.GAME_RESUMED, game=self.info.id)
        return result

    def at_checkpoint(self) -> bool:
        return self._paused or self.phase in ("intro", "reveal", "between", "finished")

    # ------------------------------------------------------------------
    # clock
    # ------------------------------------------------------------------
    def _set_phase(self, phase: str, now: float, seconds: float) -> None:
        self.phase = phase
        self.phase_started = now
        self.phase_total = seconds
        self.phase_ends = now + seconds

    def tick(self, now: float) -> StepResult:
        result = StepResult()
        if self._paused or self._done:
            return result
        if self.phase == "turn" and self.awaiting_line:
            if now >= self.line_deadline:
                result.extend(self._line_finished(now))
            return result
        if now < self.phase_ends:
            return result
        if self.phase == "intro":
            result.extend(self._start_round(now))
        elif self.phase == "question":
            # Chat's turn is over without a right answer: Mika and Luna's turn.
            if self.info.viewer_participation:
                result.event(
                    base.VIEWER_TIMEOUT, game=self.info.id, round=self.round_no
                )
                if self.chat_played and not self.viewer_attempts and self.order:
                    result.line(self.order[0], "too_slow")
            self.turn_index = 0
            result.extend(self._begin_turn(now))
        elif self.phase == "turn":
            result.extend(self._character_answer(now))
        elif self.phase == "reveal":
            if self.round_no >= self.rounds_total:
                result.extend(self._finish(now))
            else:
                self._set_phase("between", now, self.between_s)
        elif self.phase == "between":
            result.extend(self._start_round(now))
        elif self.phase == "finished":
            self._done = True
        return result

    # ------------------------------------------------------------------
    # rounds
    # ------------------------------------------------------------------
    @property
    def questions(self) -> list[dict[str, Any]]:
        return self.bank.all()  # includes AI questions added while running

    def _pick_question(self) -> dict[str, Any]:
        def pool(use_category: bool, use_difficulty: bool) -> list[dict[str, Any]]:
            return [
                q
                for q in self.questions
                if q["id"] not in self.used
                and (
                    not use_category
                    or not self.category
                    or q["category"] == self.category
                )
                and (
                    not use_difficulty
                    or self.difficulty == "mixed"
                    or q["difficulty"] == self.difficulty
                )
            ]

        for use_category, use_difficulty in (
            (True, True),
            (True, False),
            (False, True),
            (False, False),
        ):
            candidates = pool(use_category, use_difficulty)
            if candidates:
                break
        else:
            self.used.clear()  # every question was asked; start over
            candidates = list(self.questions)
            if not candidates:
                raise ValueError("no trivia questions")
        # Never asked in any stream first, else the one asked longest ago.
        question = self.bank.pick(candidates, self.rng)
        self.used.add(question["id"])
        self.bank.mark_asked(question["id"])
        return question

    def _start_round(self, now: float) -> StepResult:
        result = StepResult()
        self.round_no += 1
        self.last_question = self.question
        self.question = self._pick_question()
        self.answers = []
        self.round_winner = None
        self.first_viewer = None
        self.viewer_attempts = {}
        self.viewer_feed.clear()
        self.awaiting_line = None
        ids = [p.id for p in self.players]
        if self.next_first in ids:
            first = self.next_first
            self.next_first = None
        else:
            first = ids[(self.round_no - 1) % len(ids)]
        self.order = [first] + [i for i in ids if i != first]
        self.turn_index = 0
        window = self.viewer_window_s if self.info.viewer_participation else 0.5
        self._set_phase("question", now, window)
        self.question_shown_at = now
        result.event(base.ROUND_STARTED, round=self.round_no, rounds=self.rounds_total)
        result.event(
            base.QUESTION_SHOWN,
            round=self.round_no,
            question_id=self.question["id"],
            category=self.question["category"],
            difficulty=self.question["difficulty"],
        )
        return result

    def _player(self, player_id: str) -> Optional[PlayerSpec]:
        return next((p for p in self.players if p.id == player_id), None)

    def _begin_turn(self, now: float) -> StepResult:
        result = StepResult()
        player = self.order[self.turn_index]
        self._set_phase("turn", now, self.think_s)
        self.awaiting_line = None
        result.event(base.CHARACTER_TURN, player=player, round=self.round_no)
        return result

    def _character_answer(self, now: float) -> StepResult:
        result = StepResult()
        player_id = self.order[self.turn_index]
        spec = self._player(player_id)
        question = self.question or {}
        chance = (spec.skill if spec else {}).get(
            question.get("difficulty", "medium"), 0.6
        )
        correct = self.rng.random() < chance
        if correct:
            text = question["correct_answer"]
        else:
            given = {normalise(a["text"]) for a in self.answers}
            options = [
                w for w in question["wrong_answers"] if normalise(w) not in given
            ]
            text = self.rng.choice(options or question["wrong_answers"])
        self.answers.append({"player": player_id, "text": text, "correct": None})
        self.pending_correct = correct
        self.awaiting_line = player_id
        self.line_deadline = now + self.line_timeout_s
        result.event(
            base.CHARACTER_ANSWERED, player=player_id, answer=text, round=self.round_no
        )
        result.line(player_id, "answer", blocking=True, answer=text)
        return result

    def character_done(self, player: str, now: float) -> StepResult:
        if self._done or self.awaiting_line != player or self.phase != "turn":
            return StepResult()
        if self._paused:
            # The line finished while paused; judge it on resume via tick.
            self.line_deadline = now
            return StepResult()
        return self._line_finished(now)

    def _line_finished(self, now: float) -> StepResult:
        result = StepResult()
        player = self.awaiting_line
        self.awaiting_line = None
        if player is None:
            return result
        correct = bool(self.pending_correct)
        self.pending_correct = None
        if self.answers:
            self.answers[-1]["correct"] = correct
        if correct:
            self.scores[player] = self.scores.get(player, 0) + 1
            self.round_winner = player
            result.event(base.ANSWER_CORRECT, player=player, round=self.round_no)
            result.event(base.SCORE_CHANGED, scores=dict(self.scores))
            result.line(player, "correct")
            result.extend(self._reveal(now))
            return result
        result.event(base.ANSWER_WRONG, player=player, round=self.round_no)
        result.line(player, "wrong")
        self.turn_index += 1
        if self.turn_index < len(self.order):
            result.extend(self._begin_turn(now))
        else:
            result.extend(self._reveal(now))
        return result

    def _reveal(self, now: float) -> StepResult:
        result = StepResult()
        self.awaiting_line = None
        self.rounds_played += 1
        self._set_phase("reveal", now, self.reveal_s)
        result.event(
            base.ROUND_FINISHED,
            round=self.round_no,
            winner=self.round_winner,
            answer=(self.question or {}).get("correct_answer"),
        )
        return result

    def _finish(self, now: float) -> StepResult:
        result = StepResult()
        self._finished = True
        top = max(self.scores.values()) if self.scores else 0
        leaders = [pid for pid, score in self.scores.items() if score == top]
        self.winner = leaders[0] if len(leaders) == 1 and top > 0 else None
        characters = [p.id for p in self.players]
        loser = None
        if self.winner in characters:
            others = [c for c in characters if c != self.winner]
            loser = min(others, key=lambda c: self.scores.get(c, 0)) if others else None
        self._set_phase("finished", now, self.finish_s)
        result.event(
            base.GAME_FINISHED,
            game=self.info.id,
            winner=self.winner,
            loser=loser,
            scores=dict(self.scores),
        )
        if self.winner in characters:
            result.line(self.winner, "win")
            if loser:
                result.line(loser, "lose")
        elif self.winner == VIEWERS and characters:
            result.line(characters[0], "viewer_first")
        return result

    # ------------------------------------------------------------------
    # chat
    # ------------------------------------------------------------------
    def handle_viewer_message(
        self, username: str, text: str, now: float
    ) -> tuple[bool, StepResult]:
        result = StepResult()
        if self._done or not self.info.viewer_participation:
            return False, result
        if not looks_like_answer(text, self.max_answer_words):
            return False, result
        question = self.question
        late_phase = self.phase in ("reveal", "between", "finished") or (
            self.phase == "turn" and self.info.viewer_participation
        )
        if late_phase or self._paused:
            # Chat's turn is over: late answers never change a closed round.
            # A late right answer is swallowed and shown as "Too late!".
            recent = (
                self.question
                if self.phase in ("reveal", "turn")
                else self.last_question
            )
            if recent and is_correct(
                text,
                recent["accepted_answers"],
                recent["wrong_answers"],
                self.typo_tolerance,
            ):
                self.viewer_feed.append(
                    {
                        "user": username.strip()[:40] or "viewer",
                        "text": text.strip()[:40],
                        "correct": False,
                        "mark": "late",
                    }
                )
                return True, result
            return False, result
        if self.phase != "question" or not question:
            return False, result

        name = username.strip()[:40] or "viewer"
        attempts = self.viewer_attempts.get(name, 0)
        if attempts >= 2:
            return True, result  # anti-spam: two guesses per question
        self.viewer_attempts[name] = attempts + 1
        self.chat_played = True
        if len(self.viewer_attempts) > 500:
            self.viewer_attempts.clear()

        correct = is_correct(
            text,
            question["accepted_answers"],
            question["wrong_answers"],
            self.typo_tolerance,
        )
        self.chat_timing.append(
            {
                "round": self.round_no,
                "received_after_question_s": round(now - self.question_shown_at, 3),
                "correct": correct,
            }
        )
        self.viewer_feed.append(
            {"user": name, "text": text.strip()[:40], "correct": correct}
        )
        result.event(
            base.VIEWER_ANSWERED, username=name, correct=correct, round=self.round_no
        )
        if not correct:
            if not self.config_keep_window:
                self.phase_ends = min(self.phase_ends, now)  # chat's turn ends
            return True, result

        self.first_viewer = name
        self.round_winner = VIEWERS
        self.scores[VIEWERS] = self.scores.get(VIEWERS, 0) + 1
        self.awaiting_line = None
        self.pending_correct = None
        result.event(
            base.ANSWER_CORRECT, player=VIEWERS, username=name, round=self.round_no
        )
        result.event(base.SCORE_CHANGED, scores=dict(self.scores))
        speaker = (
            self.order[min(self.turn_index, len(self.order) - 1)]
            if self.order
            else None
        )
        if speaker:
            result.line(speaker, "viewer_first", user=name)
        result.extend(self._reveal(now))
        return True, result

    def handle_command(
        self, command: base.Command, now: float
    ) -> tuple[bool, str, StepResult]:
        result = StepResult()
        if isinstance(command, SetDifficulty):
            level = command.level
            if level in ("harder", "easier"):
                ladder = list(DIFFICULTIES)
                current = self.difficulty if self.difficulty in ladder else "medium"
                index = ladder.index(current) + (1 if level == "harder" else -1)
                level = ladder[max(0, min(len(ladder) - 1, index))]
            if level not in (*DIFFICULTIES, "mixed"):
                return False, "unknown_difficulty", result
            self.difficulty = level
            return True, "difficulty_set", result
        if isinstance(command, SetCategory):
            if command.category == "any":
                self.category = None
                return True, "category_cleared", result
            if command.category not in self.info.categories:
                return False, "unknown_category", result
            self.category = command.category
            return True, "category_set", result
        if isinstance(command, SetFirstPlayer):
            if command.player not in [p.id for p in self.players]:
                return False, "unknown_player", result
            self.next_first = command.player
            return True, "first_player_set", result
        if isinstance(command, NextRound):
            if self.phase in ("question", "turn") and not self._paused:
                self.round_winner = None
                result.extend(self._reveal(now))
                return True, "round_skipped", result
            if self.phase in ("reveal", "between", "intro") and not self._paused:
                if self.round_no >= self.rounds_total:
                    result.extend(self._finish(now))
                else:
                    result.extend(self._start_round(now))
                return True, "round_skipped", result
            return False, "cannot_skip", result
        return False, "unsupported", result

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------
    def _name(self, player_id: Optional[str]) -> str:
        if player_id == VIEWERS:
            return "Chat"
        spec = self._player(player_id or "")
        return spec.name if spec else ""

    def _status(self) -> str:
        if self._paused:
            return "Paused"
        if self.phase == "intro":
            return "Get ready!"
        if self.phase == "question":
            return "Chat, type your answer!"
        if self.phase == "turn":
            name = self._name(self.order[self.turn_index] if self.order else None)
            return (
                f"{name} answers..." if self.awaiting_line else f"{name} is thinking..."
            )
        if self.phase == "reveal":
            if self.round_winner == VIEWERS and self.first_viewer:
                return f"@{self.first_viewer.lstrip('@')} got it first!"
            if self.round_winner:
                return f"Point for {self._name(self.round_winner)}!"
            return "Nobody got it!"
        if self.phase == "between":
            return "Next question..."
        if self.winner == VIEWERS:
            return "Chat wins!"
        if self.winner:
            return f"{self._name(self.winner)} wins!"
        return "It's a tie!"

    def _turn_label(self) -> str:
        if self._paused or self._finished:
            return ""
        if self.phase == "question" and self.info.viewer_participation:
            return "CHAT'S TURN"
        if self.phase == "turn":
            names = [self._name(p) for p in self.order[self.turn_index :]]
            return " & ".join(n.upper() for n in names if n) + "'S TURN"
        return ""

    def view(self, now: float) -> dict[str, Any]:
        question = self.question if self.phase not in ("intro",) else None
        timed = self.phase in ("question", "turn") and not self.awaiting_line
        remaining = max(
            0.0, self.phase_ends - (self._pause_started if self._paused else now)
        )
        highlight = None
        if self.phase == "turn" and self.order:
            highlight = self.order[self.turn_index]
        elif self.phase in ("reveal",):
            highlight = self.round_winner
        elif self.phase == "finished":
            highlight = self.winner
        scores = [
            {"id": p.id, "name": p.name, "score": self.scores.get(p.id, 0)}
            for p in self.players
        ]
        if VIEWERS in self.scores:
            scores.append(
                {"id": VIEWERS, "name": "Chat", "score": self.scores[VIEWERS]}
            )
        return {
            "renderer": "trivia",
            "game_id": self.info.id,
            "title": self.info.display_name.upper(),
            "round": self.round_no,
            "rounds": self.rounds_total,
            "phase": self.phase,
            "status": self._status(),
            "paused": self._paused,
            "finished": self._finished,
            "winner": self.winner,
            "highlight": highlight,
            "turn_label": self._turn_label(),
            "hint": self.info.hint if self.info.viewer_participation else "",
            "timer": {
                "remaining_ms": int(remaining * 1000),
                "total_ms": int(self.phase_total * 1000),
            }
            if timed and not self._paused
            else None,
            "scores": scores,
            "body": {
                "question": question["question"] if question else "",
                "category": question["category"].title() if question else "",
                "difficulty": question["difficulty"] if question else "",
                "answers": [
                    {
                        "player": a["player"],
                        "name": self._name(a["player"]),
                        "text": a["text"],
                        "correct": a["correct"],
                    }
                    for a in self.answers
                ],
                "reveal": question["correct_answer"]
                if question and self.phase in ("reveal", "between", "finished")
                else None,
                "viewer_feed": list(self.viewer_feed),
                "first_viewer": self.first_viewer,
            },
        }

    def state(self) -> dict[str, Any]:
        return {
            "game": self.info.id,
            "phase": self.phase,
            "round": self.round_no,
            "rounds": self.rounds_total,
            "scores": dict(self.scores),
            "difficulty": self.difficulty,
            "category": self.category,
            "question_id": (self.question or {}).get("id"),
            "order": list(self.order),
            "turn_index": self.turn_index,
            "awaiting_line": self.awaiting_line,
            "round_winner": self.round_winner,
            "winner": self.winner,
            "paused": self._paused,
            "pause_reason": self.pause_reason,
            "finished": self._finished,
            "done": self._done,
            "rounds_played": self.rounds_played,
            "viewer_window_s": self.viewer_window_s,
            "chat_timing": list(self.chat_timing),
        }
