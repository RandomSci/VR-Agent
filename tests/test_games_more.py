"""Tic-Tac-Toe, Rock Paper Scissors, the registry metadata, game selection,
who-plays-whom commands, and viewer-first Trivia. All deterministic, no LLM."""

from __future__ import annotations

import random

import pytest

from open_llm_vtuber.games import GameEngine, GameRegistry, PlayerSpec, base
from open_llm_vtuber.games.commands import parse_command
from open_llm_vtuber.games.rps.game import judge, parse_hand
from open_llm_vtuber.games.tictactoe.game import best_move, parse_cell, winning_line
from open_llm_vtuber.games.voting import ChatVote

PLAYERS = [
    PlayerSpec("mika", "Mika", {"easy": 0.8, "medium": 0.55, "hard": 0.35}),
    PlayerSpec("luna", "Luna", {"easy": 0.9, "medium": 0.7, "hard": 0.45}),
]
NAMES = {"mika": "mika", "luna": "luna"}


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture(scope="module")
def registry():
    return GameRegistry.discover()


def run(engine, clock, seconds, step=0.1):
    events, lines = [], []
    for _ in range(int(seconds / step)):
        clock.advance(step)
        result = engine.tick()
        events += result.events
        lines += result.lines
    return events, lines


def names(events):
    return [e.name for e in events]


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------
def test_registry_metadata_describes_every_game(registry):
    games = {g["id"]: g for g in registry.describe()["games"]}
    assert set(games) == {"trivia", "tictactoe", "rps"}
    assert games["tictactoe"]["renderer"] == "grid"
    assert games["rps"]["renderer"] == "rps"
    assert games["trivia"]["renderer"] == "trivia"
    for game in games.values():
        assert game["how_to_play"] and game["modes"] and game["max_players"] == 2
    assert registry.find("jack en poy").info.id == "rps"
    assert registry.find("tic-tac-toe").info.id == "tictactoe"


def test_lets_play_a_game_picks_the_least_recently_played(registry):
    clock = Clock()
    engine = GameEngine(registry, clock=clock, rng=random.Random(1))
    played = []
    for _ in range(3):
        outcome = engine.start_game(None, PLAYERS)
        played.append(engine.active.info.id)
        engine.stop_game(clock(), "test")
        clock.advance(10)
    assert sorted(played) == ["rps", "tictactoe", "trivia"]  # each once before repeats
    engine.start_game(None, PLAYERS)
    assert engine.active.info.id == played[0]
    assert outcome.accepted


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,game,mode,first,opponent",
    [
        ("Girls, play tic tac toe", "tictactoe", "characters", None, None),
        ("Mika challenge Luna to trivia", "trivia", "characters", "mika", None),
        ("Mika vs Luna in rock paper scissors", "rps", "characters", "mika", None),
        ("Luna, play tic tac toe with us", "tictactoe", "chat", None, "luna"),
        ("play rock paper scissors", "rps", None, None, None),
        ("jack en poy", "rps", None, None, None),
        ("tic tac toe", "tictactoe", None, None, None),
    ],
)
def test_who_plays_whom(registry, text, game, mode, first, opponent):
    command = parse_command(text, registry, NAMES, game_active=False)
    assert isinstance(command, base.StartGame)
    assert (command.game_id, command.mode, command.first, command.opponent) == (
        game,
        mode,
        first,
        opponent,
    )


def test_rules_question_names_the_right_game(registry):
    assert parse_command(
        "how does tic tac toe work?", registry, NAMES
    ) == base.HowToPlay("tictactoe")


# ---------------------------------------------------------------------------
# voting
# ---------------------------------------------------------------------------
def test_chat_vote_counts_one_vote_per_viewer_and_marks_late_votes():
    vote = ChatVote()
    vote.start()
    vote.cast("@a", "4")
    vote.cast("@b", "2")
    vote.cast("@a", "2")  # changed their mind
    vote.cast("@c", "4")
    assert vote.counts() == {"2": 2, "4": 1}
    assert vote.close() == "2"
    assert vote.cast("@d", "4") is False
    assert vote.feed[-1]["mark"] == "late" and vote.winner() == "2"


def test_tie_goes_to_the_option_voted_first():
    vote = ChatVote()
    vote.start()
    vote.cast("@a", "8")
    vote.cast("@b", "0")
    assert vote.close() == "8"


# ---------------------------------------------------------------------------
# Tic-Tac-Toe
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,cell",
    [
        ("5", 4),
        ("#1", 0),
        ("cell 9", 8),
        ("top left", 0),
        ("center", 4),
        ("hello", None),
        ("10", None),
    ],
)
def test_parse_cell(text, cell):
    assert parse_cell(text) == cell


def test_rules_player_wins_and_blocks():
    rng = random.Random(0)
    assert (
        best_move(["O", "O", "", "X", "X", "", "", "", ""], "O", 1.0, rng) == 2
    )  # win
    assert (
        best_move(["X", "X", "", "", "O", "", "", "", ""], "O", 1.0, rng) == 2
    )  # block
    assert winning_line(["X", "X", "X", "", "", "", "", "", ""]) == (0, 1, 2)


def _ttt(registry, clock, **options):
    engine = GameEngine(registry, clock=clock, rng=random.Random(4))
    outcome = engine.start_game("tictactoe", PLAYERS, options=options)
    assert outcome.accepted
    engine.notify_viewer_activity()
    return engine


def test_chat_votes_a_cell_and_the_character_answers(registry):
    clock = Clock()
    engine = _ttt(registry, clock, opponent="luna")
    game = engine.active
    assert game.sides == ["viewers", "luna"] and game.mode == "chat"
    run(engine, clock, 3.2)  # intro
    assert game.phase == "vote" and game.turn == "viewers"
    for user in ("@a", "@b", "@c"):
        consumed, _ = engine.handle_viewer_message(user, "5", clock())
        assert consumed
    engine.handle_viewer_message("@d", "1", clock())
    view = game.view(clock())
    assert (
        view["body"]["cells"][4]["votes"] == 3 and view["turn_label"] == "CHAT'S TURN"
    )
    events, _ = run(
        engine, clock, game.quiet_close_s + 0.5
    )  # quiet period closes the vote
    assert game.cells[4] == "X"
    assert base.MOVE_MADE in names(events)
    events, _ = run(engine, clock, 2.0)  # Luna thinks, then places O
    assert game.cells.count("O") == 1
    assert game.phase == "vote"  # back to chat


def test_no_votes_means_too_slow_and_a_random_move(registry):
    clock = Clock()
    engine = _ttt(registry, clock, opponent="mika")
    game = engine.active
    run(engine, clock, 3.2)
    events, lines = run(engine, clock, game.vote_s + 0.5)
    assert base.VIEWER_TIMEOUT in names(events)
    assert any(line.kind == "too_slow" and line.player == "mika" for line in lines)
    assert game.cells.count("X") == 1


def test_votes_after_the_vote_closed_never_change_the_board(registry):
    clock = Clock()
    engine = _ttt(registry, clock, opponent="mika")
    game = engine.active
    run(engine, clock, 3.2)
    engine.handle_viewer_message("@a", "1", clock())
    run(engine, clock, game.quiet_close_s + 0.5)
    board = list(game.cells)
    consumed, result = engine.handle_viewer_message("@late", "9", clock())
    assert consumed and not result.events and game.cells == board
    assert game.vote.feed[-1]["mark"] == "late"


def test_girls_play_each_other_to_the_end(registry):
    clock = Clock()
    engine = _ttt(registry, clock, mode="characters", first="luna")
    game = engine.active
    assert game.mode == "characters" and game.sides == ["luna", "mika"]
    consumed, _ = engine.handle_viewer_message("@a", "5", clock())
    assert not consumed  # chat only watches in this mode
    events, lines = run(engine, clock, 120)
    assert base.GAME_FINISHED in names(events)
    assert game.finished and max(game.scores.values()) <= game.first_to
    assert all(line.kind != "too_slow" for line in lines)


# ---------------------------------------------------------------------------
# Rock Paper Scissors
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,hand",
    [
        ("rock", "rock"),
        ("Paper!", "paper"),
        ("✌️", "scissors"),
        ("bato", "rock"),
        ("gunting", "scissors"),
        ("hi", None),
        ("rock or paper", None),
    ],
)
def test_parse_hand(text, hand):
    assert parse_hand(text) == hand


def test_judge():
    assert judge("rock", "scissors") == 1
    assert judge("rock", "paper") == -1
    assert judge("paper", "paper") == 0


def test_rps_character_hand_is_fixed_before_chat_votes(registry):
    clock = Clock()
    engine = GameEngine(registry, clock=clock, rng=random.Random(7))
    engine.start_game("rps", PLAYERS, options={"opponent": "mika"})
    engine.notify_viewer_activity()
    game = engine.active
    run(engine, clock, 3.2)
    assert game.phase == "vote"
    hidden = dict(game.hidden)
    view = game.view(clock())
    assert all(h["hand"] is None for h in view["body"]["hands"])  # nothing shown yet
    beat = {"rock": "paper", "paper": "scissors", "scissors": "rock"}[hidden["mika"]]
    engine.handle_viewer_message("@a", beat, clock())
    assert game.hidden == hidden  # chat's votes cannot change mika's hand
    events, _ = run(engine, clock, game.quiet_close_s + 0.5)
    assert game.round_winner == "viewers" and game.scores["viewers"] == 1
    assert base.ANSWER_CORRECT in names(events)


def test_rps_no_votes_gives_the_round_to_the_character(registry):
    clock = Clock()
    engine = GameEngine(registry, clock=clock, rng=random.Random(2))
    engine.start_game("rps", PLAYERS, options={"opponent": "luna"})
    engine.notify_viewer_activity()
    game = engine.active
    run(engine, clock, 3.2)
    events, lines = run(engine, clock, game.vote_s + 0.5)
    assert base.VIEWER_TIMEOUT in names(events)
    assert (
        game.scores["luna"] == 1 and game.view(clock())["status"] == "Too slow, chat!"
    )
    assert any(line.kind == "too_slow" for line in lines)


def test_rps_girls_mode_finishes(registry):
    clock = Clock()
    engine = GameEngine(registry, clock=clock, rng=random.Random(3))
    engine.start_game("rps", PLAYERS, options={"mode": "characters"})
    engine.notify_viewer_activity()
    events, _ = run(engine, clock, 200)
    assert base.GAME_FINISHED in names(events)


# ---------------------------------------------------------------------------
# viewer-first Trivia
# ---------------------------------------------------------------------------
def _trivia(registry, clock):
    engine = GameEngine(registry, clock=clock, rng=random.Random(9))
    engine.start_game("trivia", PLAYERS)
    engine.notify_viewer_activity()
    run(engine, clock, 3.2)
    return engine


def test_chat_gets_its_own_turn_alone_and_girls_stay_silent(registry):
    clock = Clock()
    engine = _trivia(registry, clock)
    game = engine.active
    window = game.viewer_window_s
    assert game.phase == "question" and window >= 12
    assert game.view(clock())["turn_label"] == "CHAT'S TURN"
    events, lines = run(engine, clock, window - 0.5)
    assert game.phase == "question"
    assert not [
        e for e in events if e.name in (base.CHARACTER_TURN, base.CHARACTER_ANSWERED)
    ]
    assert not [line for line in lines if line.kind == "answer"]
    events, _ = run(engine, clock, 1.0)
    assert base.VIEWER_TIMEOUT in names(events)
    assert game.phase == "turn"
    assert game.view(clock())["turn_label"].endswith("'S TURN")
    assert "CHAT" not in game.view(clock())["turn_label"]


def test_chat_right_answer_locks_the_round_and_skips_the_girls(registry):
    clock = Clock()
    engine = _trivia(registry, clock)
    game = engine.active
    answer = game.question["correct_answer"]
    run(engine, clock, 6.0)
    consumed, result = engine.handle_viewer_message("@selwyn", answer, clock())
    assert consumed and game.round_winner == "viewers" and game.phase == "reveal"
    assert game.scores["viewers"] == 1
    assert not [line for line in result.lines if line.kind == "answer"]
    events, lines = run(engine, clock, 4.0)
    assert not [e for e in events if e.name == base.CHARACTER_ANSWERED]
    assert game.chat_timing[-1]["received_after_question_s"] == pytest.approx(
        6.0, abs=0.3
    )


def test_late_answers_cannot_change_a_closed_round(registry):
    clock = Clock()
    engine = _trivia(registry, clock)
    game = engine.active
    answer = game.question["correct_answer"]
    run(engine, clock, game.viewer_window_s + 0.5)  # chat's turn is over
    assert game.phase == "turn"
    scores = dict(game.scores)
    consumed, result = engine.handle_viewer_message("@late", answer, clock())
    assert consumed and not result.events and game.scores == scores
    assert game.viewer_feed[-1]["mark"] == "late"


# ---------------------------------------------------------------------------
# rules for chat: said at the start, on the board, and on request mid-game
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "how to play this",
        "I don't know how to place anything",
        "what do I do?",
        "rules",
        "how do we vote?",
    ],
)
def test_help_during_a_game_asks_for_the_running_games_rules(registry, text):
    assert parse_command(text, registry, NAMES, game_active=True) == base.HowToPlay(
        None
    )


def test_help_answers_with_the_running_games_quick_rules(registry):
    engine = GameEngine(registry, clock=Clock(), rng=random.Random(1))
    engine.start_game("tictactoe", PLAYERS)
    outcome = engine.handle_command(base.HowToPlay(None), PLAYERS)
    assert outcome.values["game_id"] == "tictactoe" and outcome.values["playing"]
    from open_llm_vtuber.room.replies import command_reply

    text = command_reply(outcome.reply, outcome.values, {})
    assert "1 to 9" in text and "let's start" not in text


def _session():
    from pathlib import Path

    from open_llm_vtuber.room.profiles import load_room
    from open_llm_vtuber.room.session import RoomSession

    root = Path(__file__).resolve().parents[1]
    return RoomSession(load_room(root / "room", root))


def _say(session, text):
    from open_llm_vtuber.room.live_message import LiveMessage

    return session.show.observe(LiveMessage("youtube", text, "@selwyn", text, 0.0))


def test_game_start_says_the_intro_then_the_rules_in_the_other_voice():
    session = _session()
    _say(session, "Luna, play tic tac toe with us")
    lines = list(session.show.lines)
    assert len(lines) >= 2
    intro, rules = lines[0], lines[1]
    assert "1 to 9" in rules.text and rules.character != intro.character


def test_mid_game_confusion_gets_the_rules_right_away():
    session = _session()
    _say(session, "play tic tac toe")
    session.show.lines.clear()
    consumed, _ops = _say(session, "I don't know how to place anything")
    assert consumed
    assert any("1 to 9" in line.text for line in session.show.lines)


def test_boards_show_how_to_play(registry):
    clock = Clock()
    for game_id, words in (("tictactoe", "1-9"), ("rps", "rock"), ("trivia", "answer")):
        engine = GameEngine(registry, clock=clock, rng=random.Random(1))
        engine.start_game(game_id, PLAYERS)
        assert words in engine.active.view(clock())["hint"]
