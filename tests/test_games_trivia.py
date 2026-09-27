"""Trivia Battle, Game Engine, Game Registry and command parsing.

All deterministic: a seeded random source and a fake clock drive the game,
so turn order, answers, scores and timers are checked exactly.
"""

from __future__ import annotations

import random

import pytest

from open_llm_vtuber.games import base
from open_llm_vtuber.games.base import PlayerSpec, StepResult, VIEWERS
from open_llm_vtuber.games.commands import parse_command
from open_llm_vtuber.games.engine import EngineSettings, GameEngine
from open_llm_vtuber.games.matching import is_correct, looks_like_answer
from open_llm_vtuber.games.registry import GameRegistry


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


PLAYERS = [
    PlayerSpec("mika", "Mika", {"easy": 0.8, "medium": 0.55, "hard": 0.35}),
    PlayerSpec("luna", "Luna", {"easy": 0.9, "medium": 0.7, "hard": 0.45}),
]
NAMES = {"mika": "mika", "luna": "luna"}


@pytest.fixture(scope="module")
def registry() -> GameRegistry:
    return GameRegistry.discover()


def names(result: StepResult) -> list[str]:
    return [e.name for e in result.events]


def run(
    engine: GameEngine,
    clock: Clock,
    seconds: float,
    step: float = 0.5,
    speech_done=True,
):
    """Advance the clock, ticking the engine and finishing blocking lines at once."""
    events: list[base.GameEvent] = []
    lines: list[base.LineRequest] = []
    for _ in range(int(seconds / step)):
        clock.advance(step)
        result = engine.tick()
        events += result.events
        lines += result.lines
        if speech_done:
            for line in result.lines:
                if line.blocking:
                    done = engine.character_done(line.player)
                    events += done.events
                    lines += done.lines
    return events, lines


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
def test_registry_lists_only_installed_games(registry):
    assert [g.id for g in (f.info for f in registry.enabled())] == [
        "rps",
        "tictactoe",
        "trivia",
    ]
    assert registry.count() == 3
    assert registry.find("trivia battle").info.id == "trivia"
    assert registry.find("quiz").info.id == "trivia"
    assert registry.find("chess") is None
    assert (
        registry.summary_sentence()
        == "We can play Rock Paper Scissors, Tic-Tac-Toe or Trivia Battle."
    )
    assert registry.count_sentence() == (
        "We currently have 3 games: Rock Paper Scissors, Tic-Tac-Toe, Trivia Battle."
    )
    info = registry.get("trivia").info
    assert {"space", "science", "geography"} <= set(info.categories)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("play trivia", base.StartGame("trivia")),
        ("Let's play trivia!", base.StartGame("trivia")),
        ("start a game", base.StartGame()),
        ("trivia time!", base.StartGame("trivia")),
        ("can you play a quiz", base.StartGame("trivia")),
        ("play chess", base.StartGame(None, "chess")),
        ("lets play tic tac toe", base.StartGame("tictactoe")),
        ("what games can you play?", base.ListGames()),
        ("how many games do you have", base.ListGames()),
        ("stop the game", base.StopGame()),
        ("no more trivia", base.StopGame()),
        ("change game", base.ChangeGame()),
        ("play something else", base.ChangeGame()),
        ("I play chess every day with my dad", None),
        ("Mars!", None),
        ("what is your favorite food?", None),
    ],
)
def test_commands_without_active_game(registry, text, expected):
    assert parse_command(text, registry, NAMES, game_active=False) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("next round", base.NextRound()),
        ("skip this question", base.NextRound()),
        ("make it harder", base.SetDifficulty("harder")),
        ("easy questions pls", base.SetDifficulty("easy")),
        ("science questions", base.SetCategory("science")),
        ("ask about planets", base.SetCategory("space")),
        ("let Mika answer first", base.SetFirstPlayer("mika")),
        ("let luna answer first", base.SetFirstPlayer("luna")),
        ("pizza questions", None),
    ],
)
def test_commands_during_game(registry, text, expected):
    cats = registry.get("trivia").info.categories
    assert (
        parse_command(text, registry, NAMES, game_active=True, categories=cats)
        == expected
    )


def test_game_only_commands_ignored_when_idle(registry):
    assert parse_command("next round", registry, NAMES, game_active=False) is None
    assert parse_command("make it harder", registry, NAMES, game_active=False) is None


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------
def test_answer_matching():
    accepted = ["Mars"]
    wrong = ["Venus", "Jupiter", "Saturn"]
    assert is_correct("Mars", accepted, wrong)
    assert is_correct("it's mars!!", accepted, wrong)
    assert is_correct("MARS", accepted, wrong)
    assert not is_correct("venus", accepted, wrong)
    assert not is_correct("mars or venus", accepted, wrong)
    assert is_correct("Canbera", ["Canberra"], ["Sydney"])  # typo tolerated
    assert not is_correct("Sydney", ["Canberra"], ["Sydney"])
    assert is_correct("8", ["Eight", "8"], ["Six"])
    assert is_correct("eight", ["Eight"], ["Six"])
    assert is_correct(
        "the blue whale", ["The blue whale", "blue whale", "whale"], ["Elephant"]
    )
    assert is_correct("0", ["Zero degrees", "0"], ["32 degrees"])
    assert looks_like_answer("Mars!", 5)
    assert not looks_like_answer("is it mars?", 5)
    assert not looks_like_answer("I think the answer might be mars but not sure", 5)


# ---------------------------------------------------------------------------
# a full game
# ---------------------------------------------------------------------------
def test_full_game_turns_scores_and_winner(registry):
    clock = Clock()
    engine = GameEngine(registry, clock=clock, rng=random.Random(7))
    outcome = engine.start_game("trivia", PLAYERS)
    assert outcome.accepted and outcome.reply == "game_started"
    assert names(outcome.result) == [base.GAME_STARTED]
    engine.notify_viewer_activity()

    all_events = list(outcome.result.events)
    turns_by_round: dict[int, list[str]] = {}
    for _ in range(200):
        events, _lines = run(engine, clock, 2.0)
        engine.notify_viewer_activity()  # an active chat keeps the game running
        all_events += events
        for e in events:
            if e.name == base.CHARACTER_TURN:
                turns_by_round.setdefault(e.data["round"], []).append(e.data["player"])
        if not engine.playing:
            break
    assert not engine.playing

    rounds = [e for e in all_events if e.name == base.ROUND_STARTED]
    assert len(rounds) == 5
    # First answerer alternates by round.
    firsts = [turns_by_round[r][0] for r in sorted(turns_by_round)]
    assert firsts[:4] == ["mika", "luna", "mika", "luna"]
    # A second turn only happens after the first character was wrong.
    for r, players in turns_by_round.items():
        assert len(players) <= 2 and len(set(players)) == len(players)

    correct = [e.data["player"] for e in all_events if e.name == base.ANSWER_CORRECT]
    finished = [e for e in all_events if e.name == base.GAME_FINISHED]
    assert len(finished) == 1
    scores = finished[0].data["scores"]
    for player in ("mika", "luna", VIEWERS):
        assert scores[player] == correct.count(player)
    top = max(scores.values())
    leaders = [p for p, s in scores.items() if s == top]
    assert finished[0].data["winner"] == (
        leaders[0] if len(leaders) == 1 and top else None
    )


def test_viewer_answers_first_and_wrong_guesses_are_consumed(registry):
    clock = Clock()
    engine = GameEngine(registry, clock=clock, rng=random.Random(3))
    engine.start_game("trivia", PLAYERS)
    run(engine, clock, 3.5)  # intro -> question
    game = engine.active
    assert game.phase == "question"
    question = game.question
    wrong = question["wrong_answers"][0]

    consumed, result = engine.handle_viewer_message("@ana", wrong)
    assert consumed and names(result) == [base.VIEWER_ANSWERED]
    assert result.events[0].data["correct"] is False
    assert game.scores[VIEWERS] == 0

    consumed, result = engine.handle_viewer_message(
        "@ben", "what's your favorite food?"
    )
    assert not consumed  # normal chat goes to the conversation director

    consumed, result = engine.handle_viewer_message("@ben", question["correct_answer"])
    assert consumed
    assert names(result)[:3] == [
        base.VIEWER_ANSWERED,
        base.ANSWER_CORRECT,
        base.SCORE_CHANGED,
    ]
    assert result.events[1].data["username"] == "@ben"
    assert game.scores[VIEWERS] == 1
    assert game.phase == "reveal"
    assert any(line.kind == "viewer_first" for line in result.lines)
    view = engine.view()
    assert view["body"]["reveal"] == question["correct_answer"]
    assert view["status"] == "@ben got it first!"

    # Late correct answers after the reveal are swallowed, no double points.
    consumed, _ = engine.handle_viewer_message("@cy", question["correct_answer"])
    assert consumed and game.scores[VIEWERS] == 1


def test_character_answer_waits_for_line_and_timeout_safety(registry):
    clock = Clock()
    engine = GameEngine(registry, clock=clock, rng=random.Random(11))
    engine.start_game("trivia", PLAYERS)
    engine.notify_viewer_activity()
    events, lines = run(engine, clock, 3.5 + 15.5 + 3.0, speech_done=False)
    game = engine.active
    assert any(line.kind == "answer" and line.blocking for line in lines)
    assert game.awaiting_line in ("mika", "luna")
    assert base.ANSWER_CORRECT not in [e.name for e in events]
    # Nobody reports the line finished; the safety timeout moves the game on.
    events, _ = run(engine, clock, 13.0, speech_done=False)
    assert {base.ANSWER_CORRECT, base.ANSWER_WRONG} & {e.name for e in events}


def test_pause_freezes_timers(registry):
    clock = Clock()
    engine = GameEngine(registry, clock=clock, rng=random.Random(5))
    engine.start_game("trivia", PLAYERS)
    run(engine, clock, 3.5)
    game = engine.active
    assert game.phase == "question"
    remaining = game.phase_ends - clock()
    engine.pause("conversation")
    clock.advance(60)
    engine.tick()
    assert game.phase == "question"
    engine.resume()
    assert game.phase_ends - clock() == pytest.approx(remaining)


def test_commands_change_game_settings(registry):
    clock = Clock()
    engine = GameEngine(registry, clock=clock, rng=random.Random(1))
    assert (
        engine.handle_command(base.ListGames(), PLAYERS)
        .values["summary"]
        .startswith("We can play Rock Paper Scissors")
    )
    unsupported = engine.handle_command(base.StartGame(None, "chess"), PLAYERS)
    assert not unsupported.accepted and unsupported.reply == "unsupported_game"
    assert unsupported.values["name"] == "chess"
    assert engine.handle_command(base.NextRound(), PLAYERS).reply == "no_game"

    assert engine.handle_command(base.StartGame("trivia"), PLAYERS).accepted
    assert (
        engine.handle_command(base.StartGame("trivia"), PLAYERS).reply
        == "already_playing"
    )
    assert (
        engine.handle_command(base.SetDifficulty("harder"), PLAYERS).values["level"]
        == "hard"
    )
    assert engine.handle_command(base.SetCategory("science"), PLAYERS).accepted
    assert engine.active.category == "science"
    assert not engine.handle_command(base.SetCategory("pizza"), PLAYERS).accepted
    assert engine.handle_command(base.SetFirstPlayer("luna"), PLAYERS).accepted
    run(engine, clock, 3.5)
    assert engine.active.order[0] == "luna"
    assert engine.active.question["category"] == "science"
    assert engine.active.question["difficulty"] == "hard"
    change = engine.handle_command(base.ChangeGame(), PLAYERS)
    assert change.accepted and change.reply == "game_started"
    assert engine.active.info.id != "trivia"  # switched to another installed game
    stop = engine.handle_command(base.StopGame(), PLAYERS)
    assert stop.accepted and base.GAME_STOPPED in names(stop.result)
    assert not engine.playing


# ---------------------------------------------------------------------------
# cost control
# ---------------------------------------------------------------------------
def test_games_never_start_by_themselves(registry):
    clock = Clock()
    engine = GameEngine(registry, clock=clock, rng=random.Random(2))
    for _ in range(3600):
        clock.advance(1)
        assert not engine.tick().events
    assert not engine.playing and engine.games_started == 0


def test_quiet_chat_pauses_then_ends_the_game(registry):
    clock = Clock()
    settings = EngineSettings(pause_after_seconds=60, end_after_seconds=150)
    engine = GameEngine(registry, clock=clock, rng=random.Random(4), settings=settings)
    engine.start_game("trivia", PLAYERS)  # a viewer asked at t=0, then left
    events, _ = run(engine, clock, 200, step=1.0)
    event_names = [e.name for e in events]
    assert base.GAME_PAUSED in event_names
    stopped = [e for e in events if e.name == base.GAME_STOPPED]
    assert stopped and stopped[0].data["reason"] == "inactive"
    assert not engine.playing
    # Nothing happens afterwards.
    events, _ = run(engine, clock, 1800, step=5.0)
    assert events == []


def test_returning_viewer_resumes_a_paused_game(registry):
    clock = Clock()
    settings = EngineSettings(pause_after_seconds=30, end_after_seconds=300)
    engine = GameEngine(registry, clock=clock, rng=random.Random(9), settings=settings)
    engine.start_game("trivia", PLAYERS)
    run(engine, clock, 60, step=1.0)
    assert engine.active.paused
    engine.notify_viewer_activity()
    result = engine.tick()
    assert base.GAME_RESUMED in names(result)


def test_broken_game_is_stopped_not_raised(registry):
    clock = Clock()
    engine = GameEngine(registry, clock=clock, rng=random.Random(1))
    engine.start_game("trivia", PLAYERS)

    def boom(now):
        raise RuntimeError("bad question")

    engine.active.tick = boom
    result = engine.tick()
    assert names(result) == [base.GAME_STOPPED]
    assert not engine.playing
