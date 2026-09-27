"""Game Engine: owns the active game, typed commands, timers and inactivity.

The engine is pure logic with an injected clock. It never talks to the LLM,
TTS, Live2D or YouTube. It returns ``StepResult`` objects (events plus
requested template lines) and ``Outcome`` objects for commands, which the
room turns into looks, sounds, board updates and speech.

Cost rules enforced here:
* games only start from a command (a viewer asked); never by themselves
* when chat goes quiet the game pauses at the next checkpoint, then ends
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import base
from .base import (
    ChangeGame,
    Command,
    Game,
    GameInfo,
    ListGames,
    PlayerSpec,
    StartGame,
    StepResult,
    StopGame,
)
from .registry import GameRegistry


@dataclass(frozen=True)
class EngineSettings:
    pause_after_seconds: float = 90.0  # no chat for this long: pause at a checkpoint
    end_after_seconds: float = 180.0  # no chat for this long: end the game
    bored_after_rounds: int = 10
    bored_after_minutes: float = 15.0
    session_gap_minutes: float = 30.0  # a new game after this gap resets boredom


@dataclass
class Outcome:
    accepted: bool
    reply: str  # template key for the room's deterministic reply
    values: dict[str, Any] = field(default_factory=dict)
    result: StepResult = field(default_factory=StepResult)


class GameEngine:
    def __init__(
        self,
        registry: GameRegistry,
        clock=time.time,
        rng: Optional[random.Random] = None,
        settings: Optional[EngineSettings] = None,
    ):
        self.registry = registry
        self.clock = clock
        self.rng = rng or random.Random()
        self.settings = settings or EngineSettings()
        self.active: Optional[Game] = None
        self.last_viewer_activity = 0.0
        self.session_started_at = 0.0
        self.session_rounds = 0
        self.last_game_ended_at = 0.0
        self.games_started = 0
        self.switch_suggested = False
        self.last_played: dict[str, float] = {}

    # ------------------------------------------------------------------
    # registry
    # ------------------------------------------------------------------
    def available_games(self) -> list[GameInfo]:
        return [f.info for f in self.registry.enabled()]

    @property
    def playing(self) -> bool:
        return self.active is not None

    def notify_viewer_activity(self, now: Optional[float] = None) -> None:
        self.last_viewer_activity = self.clock() if now is None else now

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def pick_game(self):
        """'Let's play a game': the least recently played game, ties at random.
        Local and deterministic given the random source; no LLM decides."""
        games = self.registry.enabled()
        if not games:
            return None
        oldest = min(self.last_played.get(g.info.id, 0.0) for g in games)
        pool = [g for g in games if self.last_played.get(g.info.id, 0.0) == oldest]
        return self.rng.choice(pool)

    def start_game(
        self,
        game_id: Optional[str],
        players: list[PlayerSpec],
        now: Optional[float] = None,
        options: Optional[dict[str, Any]] = None,
    ) -> Outcome:
        now = self.clock() if now is None else now
        factory = self.registry.get(game_id) if game_id else self.pick_game()
        if not factory:
            return Outcome(False, "unsupported_game", {"name": game_id or "that game"})
        if self.active and not self.active.finished:
            return Outcome(
                False, "already_playing", {"game": self.active.info.display_name}
            )
        if not players:
            return Outcome(False, "no_players")
        game = factory.create(rng=self.rng, options=options or {})
        result = game.start(players, now)
        self.active = game
        self.last_played[factory.info.id] = now
        self.games_started += 1
        self.switch_suggested = False
        if not self.session_started_at or (
            now - self.last_game_ended_at > self.settings.session_gap_minutes * 60
            and self.last_game_ended_at
        ):
            self.session_started_at = now
            self.session_rounds = 0
        self.last_viewer_activity = max(self.last_viewer_activity, now)
        return Outcome(
            True, "game_started", {"game": factory.info.display_name}, result
        )

    def stop_game(
        self, now: Optional[float] = None, reason: str = "stopped"
    ) -> Outcome:
        now = self.clock() if now is None else now
        if not self.active:
            return Outcome(False, "no_game")
        game = self.active
        result = game.stop(now, reason)
        scores = dict(game.state().get("scores") or {})
        self._clear(now)
        return Outcome(
            True,
            "game_stopped",
            {"game": game.info.display_name, "scores": scores, "reason": reason},
            result,
        )

    def switch_game(
        self,
        game_id: Optional[str],
        players: list[PlayerSpec],
        now: Optional[float] = None,
    ) -> Outcome:
        now = self.clock() if now is None else now
        target = self.registry.get(game_id) if game_id else None
        others = [
            f
            for f in self.registry.enabled()
            if not self.active or f.info.id != self.active.info.id
        ]
        if game_id and not target:
            return Outcome(False, "unsupported_game", {"name": game_id})
        if not target:
            if not others:
                return Outcome(
                    False,
                    "no_other_games",
                    {"summary": self.registry.summary_sentence()},
                )
            target = others[0]
        if self.active and self.active.info.id == target.info.id:
            return Outcome(False, "already_playing", {"game": target.info.display_name})
        result = StepResult()
        if self.active:
            result.extend(self.stop_game(now, "switch").result)
        outcome = self.start_game(target.info.id, players, now)
        outcome.result = result.extend(outcome.result)
        return outcome

    def _clear(self, now: float) -> None:
        if self.active:
            self.session_rounds += int(self.active.state().get("rounds_played") or 0)
        self.active = None
        self.last_game_ended_at = now

    # ------------------------------------------------------------------
    # commands and chat
    # ------------------------------------------------------------------
    def handle_command(
        self, command: Command, players: list[PlayerSpec], now: Optional[float] = None
    ) -> Outcome:
        now = self.clock() if now is None else now
        self.notify_viewer_activity(now)
        if isinstance(command, ListGames):
            return Outcome(
                True,
                "games_list",
                {
                    "summary": self.registry.summary_sentence(),
                    "count": self.registry.count(),
                },
            )
        if isinstance(command, base.HowToPlay):
            factory = self.registry.get(command.game_id)
            if factory is None and self.active is not None:
                factory = self.registry.get(self.active.info.id)
            factory = factory or self.registry.default()
            if not factory:
                return Outcome(
                    False, "games_list", {"summary": self.registry.summary_sentence()}
                )
            return Outcome(
                True,
                "how_to_play",
                {
                    "game": factory.info.display_name,
                    "game_id": factory.info.id,
                    "rules": factory.info.how_to_play or factory.info.description,
                    "quick_rules": factory.info.quick_rules,
                    "playing": bool(
                        self.active and self.active.info.id == factory.info.id
                    ),
                },
            )
        if isinstance(command, StartGame):
            if command.requested_name and not command.game_id:
                return Outcome(
                    False,
                    "unsupported_game",
                    {
                        "name": command.requested_name,
                        "summary": self.registry.summary_sentence(),
                    },
                )
            options = {
                k: v
                for k, v in (
                    ("mode", command.mode),
                    ("opponent", command.opponent),
                    ("first", command.first),
                )
                if v
            }
            return self.start_game(command.game_id, players, now, options=options)
        if isinstance(command, StopGame):
            return self.stop_game(now, "viewer")
        if isinstance(command, ChangeGame):
            if command.requested_name and not command.game_id:
                return Outcome(
                    False,
                    "unsupported_game",
                    {
                        "name": command.requested_name,
                        "summary": self.registry.summary_sentence(),
                    },
                )
            if not self.active:
                return (
                    self.start_game(command.game_id, players, now)
                    if command.game_id
                    else Outcome(
                        True,
                        "games_list",
                        {
                            "summary": self.registry.summary_sentence(),
                            "count": self.registry.count(),
                        },
                    )
                )
            return self.switch_game(command.game_id, players, now)
        if isinstance(command, base.GAME_ONLY_COMMANDS):
            if not self.active:
                return Outcome(False, "no_game")
            accepted, reply, result = self.active.handle_command(command, now)
            values = self._command_values(command)
            return Outcome(accepted, reply, values, result)
        return Outcome(False, "unsupported")

    def _command_values(self, command: Command) -> dict[str, Any]:
        values: dict[str, Any] = {}
        state = self.active.state() if self.active else {}
        if isinstance(command, base.SetDifficulty):
            values["level"] = state.get("difficulty", command.level)
        if isinstance(command, base.SetCategory):
            values["category"] = command.category
            if self.active:
                values["categories"] = list(self.active.info.categories)
        if isinstance(command, base.SetFirstPlayer):
            values["player"] = command.player
        return values

    def handle_viewer_message(
        self, username: str, text: str, now: Optional[float] = None
    ) -> tuple[bool, StepResult]:
        now = self.clock() if now is None else now
        self.notify_viewer_activity(now)
        if not self.active:
            return False, StepResult()
        return self.active.handle_viewer_message(username, text, now)

    def character_done(self, player: str, now: Optional[float] = None) -> StepResult:
        now = self.clock() if now is None else now
        if not self.active:
            return StepResult()
        return self.active.character_done(player, now)

    def pause(self, reason: str, now: Optional[float] = None) -> StepResult:
        now = self.clock() if now is None else now
        return self.active.pause(now, reason) if self.active else StepResult()

    def resume(self, now: Optional[float] = None) -> StepResult:
        now = self.clock() if now is None else now
        return self.active.resume(now) if self.active else StepResult()

    # ------------------------------------------------------------------
    # clock
    # ------------------------------------------------------------------
    def tick(self, now: Optional[float] = None) -> StepResult:
        now = self.clock() if now is None else now
        result = StepResult()
        game = self.active
        if not game:
            return result
        try:
            quiet = now - self.last_viewer_activity
            if not game.finished:
                if quiet >= self.settings.end_after_seconds and (
                    game.at_checkpoint()
                    or quiet >= self.settings.end_after_seconds + 60
                ):
                    return self.stop_game(now, "inactive").result
                if (
                    quiet >= self.settings.pause_after_seconds
                    and not game.paused
                    and game.at_checkpoint()
                ):
                    result.extend(game.pause(now, "no_viewers"))
                elif (
                    game.paused
                    and getattr(game, "pause_reason", "") == "no_viewers"
                    and quiet < self.settings.pause_after_seconds
                ):
                    result.extend(game.resume(now))
            result.extend(game.tick(now))
            if getattr(game, "done", False):
                self._clear(now)
        except Exception as exc:
            # A broken game must never stop the show.
            stopped = StepResult()
            stopped.event(
                base.GAME_STOPPED, game=game.info.id, reason=f"error: {exc}"[:120]
            )
            self._clear(now)
            return stopped
        return result

    # ------------------------------------------------------------------
    # boredom (local counters only)
    # ------------------------------------------------------------------
    def rounds_this_session(self) -> int:
        played = (
            int(self.active.state().get("rounds_played") or 0) if self.active else 0
        )
        return self.session_rounds + played

    def switch_suggestion_available(self, now: Optional[float] = None) -> bool:
        now = self.clock() if now is None else now
        if not self.active or self.switch_suggested or not self.active.at_checkpoint():
            return False
        long_session = (
            now - self.session_started_at
        ) >= self.settings.bored_after_minutes * 60
        many_rounds = self.rounds_this_session() >= self.settings.bored_after_rounds
        return bool(self.session_started_at) and (long_session or many_rounds)

    def mark_switch_suggested(self) -> None:
        self.switch_suggested = True

    # ------------------------------------------------------------------
    # views
    # ------------------------------------------------------------------
    def view(self, now: Optional[float] = None) -> Optional[dict[str, Any]]:
        now = self.clock() if now is None else now
        return self.active.view(now) if self.active else None

    def current_state(self) -> dict[str, Any]:
        return {
            "active": self.active.state() if self.active else None,
            "available": [g.describe() for g in self.available_games()],
            "last_viewer_activity": self.last_viewer_activity or None,
            "rounds_this_session": self.rounds_this_session(),
            "games_started": self.games_started,
        }
