"""Fixes from a real stream: characters answer each other, spoken text keeps its
spaces and loses stray quotes, and games start from natural chat ("Sure")."""

from __future__ import annotations

import asyncio
import random
from pathlib import Path

from open_llm_vtuber.conversations.conversation_utils import join_spoken
from open_llm_vtuber.games import GameRegistry
from open_llm_vtuber.games.base import HowToPlay, ListGames, StartGame
from open_llm_vtuber.games.commands import parse_command
from open_llm_vtuber.room.director import asks_character
from open_llm_vtuber.room.live_message import LiveMessage
from open_llm_vtuber.room.profiles import load_room
from open_llm_vtuber.room.prompting import strip_wrapping_quotes
from open_llm_vtuber.room.session import RoomSession

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = GameRegistry.discover()


def make_session(seed: int = 1) -> RoomSession:
    return RoomSession(load_room(ROOT / "room", ROOT), rng=random.Random(seed))


def msg(text: str, user: str = "@selwyn") -> LiveMessage:
    return LiveMessage("youtube", f"m-{text}", user, text, 0.0, author_id="UCselwyn")


class NoFollowUp(random.Random):
    def random(self):
        return 0.99  # above follow_up_chance: no random follow-up turn


class ScriptedRunner:
    """Returns scripted lines per call, in order."""

    def __init__(self, lines):
        self.lines = list(lines)
        self.calls = []

    async def __call__(self, turn, prompt, plan):
        self.calls.append((turn.speaker, turn.kind, prompt))
        return self.lines.pop(0) if self.lines else f"{turn.speaker} ok."


def run(session, text, runner):
    session.director.turn_runner = runner
    plan = session.director.plan(msg(text))
    return asyncio.run(session.director.run(plan))


# ---------------------------------------------------------------------------
# characters talk to each other
# ---------------------------------------------------------------------------
def test_asks_character_detects_questions_by_name():
    luna = load_room(ROOT / "room", ROOT).get("luna")
    assert asks_character("It's Scarlet Witch! What do you think, Luna?", luna)
    assert asks_character("Luna, would you like to go first?", luna)
    assert not asks_character("Luna is great. Right?", luna)
    assert not asks_character("What do you think, chat?", luna)


def test_second_character_answers_the_question_mika_asked_her():
    session = make_session()
    runner = ScriptedRunner(
        [
            "Oh, that's easy! It's Scarlet Witch! What do you think, Luna?",
            "Wanda is strong, but my vote goes to Doctor Strange.",
        ]
    )
    plan = run(session, "Who is the most powerful hero in avengers", runner)
    assert len(runner.calls) == 2
    luna_prompt = runner.calls[1][2]
    assert "asked you" in luna_prompt and "Answer" in luna_prompt
    assert "Who is the most powerful hero in avengers" in luna_prompt
    assert all(t.text for t in plan.turns)


def test_a_question_to_the_other_character_gets_a_bounded_reply():
    session = make_session()
    session.director.rng = NoFollowUp()
    runner = ScriptedRunner(
        [
            "Fantastic! Mika, would you like to ask the first question?",
            "Ooh yes! Luna, do you want to go second?",
            "Sure, I'll go second.",
            "never called",
        ]
    )
    plan = run(session, "Luna, lets talk", runner)
    speakers = [t.speaker for t in plan.turns if t.text]
    assert speakers == ["luna", "mika", "luna"]
    assert len(plan.turns) <= session.room.director.max_turns
    # the last allowed turn is told not to ask back
    assert "Do not ask another question back" in runner.calls[-1][2]


def test_added_reply_turn_is_skipped_when_a_viewer_is_waiting():
    session = make_session()
    session.director.rng = NoFollowUp()
    session.director.pending_probe = lambda: True
    runner = ScriptedRunner(["Mika, want to go first?"])
    plan = run(session, "Luna, hi", runner)
    assert [t.speaker for t in plan.turns if t.text] == ["luna"]
    assert plan.turns[-1].skipped == "viewer waiting"


# ---------------------------------------------------------------------------
# spoken text
# ---------------------------------------------------------------------------
def test_join_spoken_keeps_spaces_between_sentences():
    text = ""
    for chunk in ["Oh,", "that's easy!", "It's gotta be Scarlet Witch!", "✨"]:
        text = join_spoken(text, chunk)
    assert text == "Oh, that's easy! It's gotta be Scarlet Witch! ✨"
    assert join_spoken("Hi", ", there") == "Hi, there"


def test_wrapping_quotes_are_removed_but_real_apostrophes_stay():
    assert strip_wrapping_quotes("'Oh, that's easy!'") == "Oh, that's easy!"
    assert strip_wrapping_quotes("''Fantastic! Mika?'") == "Fantastic! Mika?"
    assert strip_wrapping_quotes('"Hi there!"') == "Hi there!"
    assert strip_wrapping_quotes("'cause I said so") == "'cause I said so"
    assert strip_wrapping_quotes("I love the girls'") == "I love the girls'"
    assert strip_wrapping_quotes('He said "hi" to me.') == 'He said "hi" to me.'


def test_prompt_tells_the_model_not_to_quote_itself():
    from open_llm_vtuber.room.prompting import RULES

    assert "never wrapped in quotation marks" in RULES


# ---------------------------------------------------------------------------
# games from natural chat
# ---------------------------------------------------------------------------
def test_parse_follow_ups_after_an_offer():
    p = lambda text, offered=None: parse_command(  # noqa: E731
        text, REGISTRY, {}, False, (), offered=offered
    )
    assert p("what games can you play?") == ListGames()
    assert p("Trivia game? How does it work? I'm interested") == HowToPlay("trivia")
    assert p("how do I play?", "trivia") == HowToPlay("trivia")
    for yes in ("Sure", "yes please!", "Lets start", "I'm in", "ok let's go!!"):
        assert p(yes, "trivia") == StartGame(game_id="trivia"), yes
    assert p("Sure") is None  # nothing was offered
    assert p("Lets start") == StartGame()  # plain start still starts a game
    assert p("Who is the most powerful hero in avengers", "trivia") is None
    assert p("is it okay if I ask first", "trivia") is None


def _observe(session, text):
    consumed, _ops = session.show.observe(msg(text))
    return consumed


def test_real_stream_chat_starts_trivia_without_the_llm():
    session = make_session()
    session.state.characters  # both available by default
    assert _observe(session, "what games can you play?")
    assert session.show.current_offer() is None  # three games: nothing to say yes to
    assert _observe(session, "Trivia game? How does it work? I'm interested")
    assert session.show.current_offer() == "trivia"
    spoken = [line.text for line in session.show.lines]
    assert any("chat's turn first" in t and "let's start" in t for t in spoken)
    assert not session.show.engine.playing
    assert _observe(session, "Sure")
    assert session.show.engine.playing
    assert session.show.engine.active.info.id == "trivia"


def test_a_character_mentioning_trivia_offers_it():
    session = make_session()
    runner = ScriptedRunner(["We could play Trivia Battle! Want to?"])
    run(session, "Mika, I'm bored", runner)
    assert session.show.current_offer() == "trivia"
    assert _observe(session, "yes!")
    assert session.show.engine.playing


def test_offer_expires():
    session = make_session()
    now = [1000.0]
    session.show.clock = lambda: now[0]
    session.show.offer("trivia")
    now[0] += session.show.OFFER_SECONDS + 1
    assert session.show.current_offer() is None
    assert not _observe(session, "sure")


def test_game_talk_gets_grounded_rules_in_the_prompt():
    session = make_session()
    runner = ScriptedRunner(["It's easy!"])
    run(session, "Mika how do you play games here?", runner)
    prompt = runner.calls[0][2]
    assert "Trivia Battle (" in prompt and "Never invent other rules" in prompt
    runner = ScriptedRunner(["Hi!"])
    run(session, "Mika what's your favorite food?", runner)
    assert "Games the stream can run" not in runner.calls[0][2]
