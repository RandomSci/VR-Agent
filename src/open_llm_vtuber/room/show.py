"""ShowRunner: runs games inside the room and turns game results into a show.

* Game events go through the room event bus (attention, reactions, objects),
  become sound effect ops, and refresh the Game Board view model.
* Template lines requested by the game are spoken in the character's voice
  only while speech is allowed (a viewer chatted recently). Otherwise they
  are shown as captions and the game moves on. No LLM is ever called here.
* Chat is checked for game commands and answers before anything else; what
  the game consumes never reaches the LLM.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Deque, Optional

from loguru import logger

from ..games import GameEngine, GameRegistry, PlayerSpec, StepResult, parse_command
from ..games.commands import mentioned_game
from ..games.base import LineRequest
from ..games.engine import EngineSettings, Outcome
from . import events as ev
from .live_message import LiveMessage
from .replies import command_reply, game_line

if TYPE_CHECKING:  # pragma: no cover
    from .session import RoomSession

# Game event -> sound effect name (files in frontend/vr-agent/sfx).
SFX_FOR_EVENT = {
    ev.GAME_STARTED: "game_start",
    ev.QUESTION_SHOWN: "question",
    ev.ANSWER_CORRECT: "correct",
    ev.ANSWER_WRONG: "wrong",
    ev.SCORE_CHANGED: "score",
    ev.GAME_FINISHED: "game_win",
    ev.GAME_STOPPED: "game_stop",
    ev.MOVE_MADE: "place",
    ev.VIEWER_TIMEOUT: "timeout",
}
SFX_NAMES = tuple(sorted(set(SFX_FOR_EVENT.values()) | {"round_win", "draw"}))


@dataclass
class QueuedLine:
    character: str
    text: str
    blocking: bool
    created: float
    addressee: Optional[str] = None
    notify_game: bool = False


class ShowRunner:
    MAX_OPTIONAL_LINE_AGE = 6.0
    OFFER_SECONDS = 180.0  # "sure" / "let's start" count as yes for this long

    def __init__(
        self,
        session: "RoomSession",
        registry: Optional[GameRegistry] = None,
        settings: Optional[EngineSettings] = None,
        rng: Optional[random.Random] = None,
        clock=time.time,
    ):
        self.session = session
        self.clock = clock
        self.rng = rng or random.Random()
        try:
            self.registry = registry or GameRegistry.discover()
        except Exception as exc:  # pragma: no cover - defensive
            logger.error(f"VR Room: game registry unavailable: {exc}")
            self.registry = GameRegistry()
        self.engine = GameEngine(
            self.registry, clock=clock, rng=self.rng, settings=settings
        )
        self.lines: Deque[QueuedLine] = deque()
        self._line_event = asyncio.Event()
        self._worker: Optional[asyncio.Task] = None
        self._last_board: Optional[dict[str, Any]] = None
        self.lines_shown_silently = 0
        self.sleep = asyncio.sleep  # injectable for simulated-time tests
        self._pending_camera: list[dict[str, Any]] = []
        self.offered_game: Optional[str] = None
        self.offered_until = 0.0
        self.chat_ingestion: Deque[dict[str, Any]] = deque(maxlen=100)

    # ------------------------------------------------------------------
    # game offers: a game that was just mentioned can be started with "sure"
    # ------------------------------------------------------------------
    def offer(self, game_id: Optional[str], now: Optional[float] = None) -> None:
        if not game_id or not self.registry.get(game_id):
            return
        self.offered_game = game_id
        self.offered_until = (self.clock() if now is None else now) + self.OFFER_SECONDS

    def current_offer(self, now: Optional[float] = None) -> Optional[str]:
        now = self.clock() if now is None else now
        if self.offered_game and now <= self.offered_until:
            return self.offered_game
        return None

    def note_text(self, text: str, now: Optional[float] = None) -> None:
        """Any line in the room (viewer or character) that names a game offers it."""
        if self.engine.playing:
            return
        game_id = mentioned_game(text or "", self.registry)
        if game_id:
            self.offer(game_id, now)

    # ------------------------------------------------------------------
    # players and names
    # ------------------------------------------------------------------
    def players(self) -> list[PlayerSpec]:
        available = set(self.session.state.available_characters())
        return [
            PlayerSpec(p.id, p.name, dict(p.game_skill))
            for p in self.session.room.characters
            if p.id in available
        ]

    def name_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for profile in self.session.room.characters:
            for name in profile.names:
                out[name] = profile.id
        return out

    def display_names(self) -> dict[str, str]:
        names = {p.id: p.name for p in self.session.room.characters}
        names["viewers"] = "Chat"
        return names

    # ------------------------------------------------------------------
    # chat
    # ------------------------------------------------------------------
    def observe(self, message: LiveMessage) -> tuple[bool, list[dict[str, Any]]]:
        """Game answers and game commands. Returns (consumed, ops)."""
        now = self.clock()
        text = message.clean_text
        if not text or message.is_system:
            return False, []
        playing = self.engine.playing
        categories = self.engine.active.info.categories if playing else ()
        command = parse_command(
            text,
            self.registry,
            self.name_map(),
            playing,
            categories,
            offered=None if playing else self.current_offer(now),
        )
        if command is None:
            self.note_text(text, now)
        if command is not None:
            outcome = self.engine.handle_command(command, self.players(), now)
            if outcome.reply == "how_to_play":
                self.offer(outcome.values.get("game_id"), now)
            elif outcome.reply == "games_list" and self.registry.count() == 1:
                self.offer(self.registry.default().info.id, now)
            elif outcome.reply == "game_started":
                self.offered_game = None
            ops = self.apply(outcome.result)
            self._reply(outcome, message)
            logger.info(
                f"VR Room game command from {message.display_name}: {type(command).__name__} -> {outcome.reply}"
            )
            return True, ops
        if playing:
            game = self.engine.active
            shown = float(getattr(game, "question_shown_at", 0.0) or 0.0)
            consumed, result = self.engine.handle_viewer_message(
                message.display_name, text, now
            )
            if consumed:
                self._record_ingestion(message, shown, now)
                return True, self.apply(result)
        return False, []

    def _record_ingestion(self, message: LiveMessage, shown: float, now: float) -> None:
        """How long a game answer took to reach the engine, stage by stage."""
        received = time.time()
        sample: dict[str, Any] = {"user": message.display_name[:30]}
        if message.timestamp and message.dom_at:
            sample["youtube_to_page_s"] = round(message.dom_at - message.timestamp, 3)
        if message.dom_at and message.detected_at:
            sample["page_to_reader_s"] = round(message.detected_at - message.dom_at, 3)
        if message.detected_at:
            sample["reader_to_engine_s"] = round(received - message.detected_at, 3)
        if shown:
            # engine clock: question shown -> answer received
            sample["question_to_engine_s"] = round(now - shown, 3)
        self.chat_ingestion.append(sample)
        logger.info(f"VR Room game answer timing: {sample}")

    def ingestion_summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"samples": len(self.chat_ingestion)}
        for key in (
            "youtube_to_page_s",
            "page_to_reader_s",
            "reader_to_engine_s",
            "question_to_engine_s",
        ):
            values = sorted(s[key] for s in self.chat_ingestion if key in s)
            if values:
                out[key] = {
                    "min": values[0],
                    "median": values[len(values) // 2],
                    "max": values[-1],
                }
        return out

    def _reply(self, outcome: Outcome, message: LiveMessage) -> None:
        text = command_reply(outcome.reply, outcome.values, self.display_names())
        if not text:
            return
        speaker = self.session.addressed_or_primary(message.clean_text)
        if speaker:
            self.enqueue(speaker, text, blocking=False, optional=False)

    # ------------------------------------------------------------------
    # game results -> show
    # ------------------------------------------------------------------
    def apply(self, result: StepResult) -> list[dict[str, Any]]:
        ops: list[dict[str, Any]] = []
        sounds: list[str] = []
        for event in result.events:
            try:
                ops += self.session.emit(event.name, **event.data)
            except ValueError:
                logger.warning(f"VR Room: unknown game event {event.name}")
                continue
            sound = SFX_FOR_EVENT.get(event.name)
            if (
                event.name == ev.ANSWER_CORRECT
                and event.data.get("player") == "viewers"
            ):
                sound = "round_win"
            if (
                event.name == ev.ROUND_FINISHED
                and event.data.get("winner") is None
                and self.engine.active is not None
                and self.engine.active.info.id != "trivia"
            ):
                sound = "draw"
            if sound:
                sounds.append(sound)
        # One effect at a time: the most important sound of this step wins.
        if sounds:
            priority = [
                "game_win",
                "game_start",
                "game_stop",
                "timeout",
                "round_win",
                "correct",
                "draw",
                "wrong",
                "place",
                "question",
                "score",
            ]
            best = min(sounds, key=lambda s: priority.index(s) if s in priority else 99)
            ops.append({"op": "sfx", "name": best})
        board = self.board_op(force=bool(result.events))
        if board:
            ops.append(board)
        for line in result.lines:
            self._queue_game_line(line)
        return ops

    def board_op(self, force: bool = False) -> Optional[dict[str, Any]]:
        view = self.engine.view()
        if not force and view == self._last_board:
            return None
        if view is None and self._last_board is None:
            return None
        cleared = view is None and self._last_board is not None
        self._last_board = view
        if cleared:
            self._pending_camera = self.session.camera.game_cleared()
        return {"op": "board", "view": view}

    def _queue_game_line(self, line: LineRequest) -> None:
        profile = self.session.room.get(line.player)
        text = game_line(profile, line.kind, line.values, self.rng)
        if not text:
            if line.blocking:
                self.enqueue(
                    line.player, "", blocking=True, optional=False, notify_game=True
                )
            return
        self.enqueue(
            line.player,
            text,
            blocking=line.blocking,
            optional=not line.blocking,
            notify_game=line.blocking,
        )

    def enqueue(
        self,
        character: str,
        text: str,
        blocking: bool,
        optional: bool,
        notify_game: bool = False,
        addressee: Optional[str] = None,
    ) -> None:
        if optional and len(self.lines) >= 2:
            return  # do not build a backlog of chatter
        self.lines.append(
            QueuedLine(
                character,
                text,
                blocking or not optional,
                self.clock(),
                addressee,
                notify_game,
            )
        )
        self._line_event.set()
        self.ensure_worker()

    # ------------------------------------------------------------------
    # line worker: one line at a time, speech only when allowed
    # ------------------------------------------------------------------
    def ensure_worker(self) -> None:
        if self._worker and not self._worker.done():
            return
        try:
            self._worker = asyncio.get_running_loop().create_task(
                self._run_lines(), name="vr-room-lines"
            )
        except RuntimeError:
            self._worker = None

    async def _run_lines(self) -> None:
        while True:
            if not self.lines:
                self._line_event.clear()
                await self._line_event.wait()
                continue
            line = self.lines.popleft()
            try:
                await self.play_line(line)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.error(f"VR Room: line failed: {exc}")

    async def play_line(self, line: QueuedLine) -> None:
        now = self.clock()
        if not line.blocking and now - line.created > self.MAX_OPTIONAL_LINE_AGE:
            return
        spoken = False
        if line.text and self.session.speech_allowed(now):
            spoken = await self.session.speech.say(
                line.character, line.text, line.addressee
            )
        if line.text and not spoken:
            # No viewers around (or TTS failed): show the line, no API call.
            self.lines_shown_silently += 1
            ms = int(min(4500, 1200 + len(line.text) * 45))
            await self.session.push(
                [
                    {
                        "op": "line",
                        "character": line.character,
                        "text": line.text,
                        "ms": ms,
                    }
                ]
            )
            await self.sleep(ms / 1000)
        if line.notify_game:
            await self.session.push(
                self.apply(self.engine.character_done(line.character, self.clock()))
            )

    # ------------------------------------------------------------------
    # clock
    # ------------------------------------------------------------------
    def tick(self) -> list[dict[str, Any]]:
        ops = [op for op in self._pending_camera if op]
        self._pending_camera = []
        if not self.engine.playing and self._last_board is None:
            return ops
        ops += self.apply(self.engine.tick(self.clock()))
        board = self.board_op()
        if board:
            ops.append(board)
        ops += [op for op in self._pending_camera if op]
        self._pending_camera = []
        ops += self._maybe_bored()
        return ops

    def _maybe_bored(self) -> list[dict[str, Any]]:
        now = self.clock()
        if not self.session.speech_allowed(
            now
        ) or not self.engine.switch_suggestion_available(now):
            return []
        self.engine.mark_switch_suggested()
        players = [p.id for p in self.players()]
        if not players:
            return []
        first = players[0]
        self._queue_game_line(LineRequest(first, "bored"))
        if len(players) > 1:
            self._queue_game_line(LineRequest(players[1], "bored"))
        others = [
            g.display_name
            for g in self.engine.available_games()
            if g.id != self.engine.active.info.id
        ]
        if others:
            text = f"Chat, want to switch to {others[0]}?"
        else:
            text = f"{self.registry.summary_sentence()} Keep going, or say stop the game for a break!"
        self.enqueue(first, text, blocking=False, optional=False)
        return []

    def at_checkpoint(self) -> bool:
        return not self.engine.playing or self.engine.active.at_checkpoint()

    def status(self) -> dict[str, Any]:
        return {
            "engine": self.engine.current_state(),
            "registry": self.registry.describe(),
            "queued_lines": len(self.lines),
            "lines_shown_silently": self.lines_shown_silently,
            "chat_timing": self.ingestion_summary(),
        }
