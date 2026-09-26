"""Trivia variety: a bigger curated bank, a saved no-repeat rotation, and AI
questions that are checked before use and only requested when a viewer
starts a trivia game."""

from __future__ import annotations

import asyncio
import json
import random
import re
from pathlib import Path

import pytest

from open_llm_vtuber.games.trivia.bank import (
    QuestionBank,
    clean_question,
    normalise_text,
)
from open_llm_vtuber.games.trivia.game import TriviaBattle, load_questions
from open_llm_vtuber.room.question_maker import QuestionMaker, parse_questions

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = ROOT / "src/open_llm_vtuber/games/trivia/questions.json"


def curated():
    return load_questions(QUESTIONS)


def test_curated_bank_is_big_varied_and_clean():
    questions = curated()
    assert len(questions) >= 250
    assert len({normalise_text(q["question"]) for q in questions}) == len(questions)
    assert len({q["id"] for q in questions}) == len(questions)
    categories = {q["category"] for q in questions}
    assert {"movies", "music", "sports", "food", "history"} <= categories
    for q in questions:
        assert q["question"].endswith("?"), q["question"]
        assert len(q["correct_answer"].split()) <= 5, q["question"]
        answer = normalise_text(q["correct_answer"])
        wrong = {normalise_text(w) for w in q["wrong_answers"]}
        assert answer not in wrong, q["question"]
        if len(answer) > 3:  # short answers ("L", "7") can appear inside other words
            assert not re.search(
                rf"\b{re.escape(answer)}\b", normalise_text(q["question"])
            ), q["question"]


def test_rotation_never_repeats_until_everything_was_asked(tmp_path):
    bank = QuestionBank(curated()[:30], cache_dir=tmp_path)
    info = (
        __import__("open_llm_vtuber.games.trivia.game", fromlist=["load"])
        .load({"cache_dir": str(tmp_path / "unused")}, QUESTIONS.parent)
        .info
    )
    asked = []
    rng = random.Random(1)
    for _ in range(6):  # six games of five rounds = the whole pool once
        game = TriviaBattle(info, {"default_difficulty": "mixed"}, bank, rng=rng)
        for _ in range(5):
            asked.append(game._pick_question()["id"])
    assert len(asked) == 30 and len(set(asked)) == 30

    # The rotation is saved: a fresh bank (a restart) keeps going from there.
    again = QuestionBank(curated()[:30], cache_dir=tmp_path)
    assert again.unseen_count() == 0
    for index, question_id in enumerate(
        asked
    ):  # spread the times out: asked[0] is oldest
        again.seen[question_id] = 1000.0 + index * 60
    game = TriviaBattle(info, {"default_difficulty": "mixed"}, again, rng=rng)
    assert (
        game._pick_question()["id"] == asked[0]
    )  # the one asked longest ago comes back first


GOOD = {
    "category": "space",
    "difficulty": "easy",
    "question": "Which planet has a day longer than its year?",
    "correct_answer": "Venus",
    "accepted_answers": ["planet venus"],
    "wrong_answers": ["Mercury", "Mars", "Jupiter"],
}


@pytest.mark.parametrize(
    "change",
    [
        {
            "question": "Is Venus the planet with a day longer than its year?"
        },  # gives it away
        {"correct_answer": "The second planet from our Sun"},  # too long to type
        {"category": "politics"},  # not a known category
        {"wrong_answers": ["Venus", "Mars"]},  # a wrong answer equals the right one
        {"question": "No question mark here"},
        {"wrong_answers": ["Mars"]},  # too few wrong answers
    ],
)
def test_bad_ai_questions_are_rejected(change):
    assert clean_question({**GOOD, **change}, ["space"], "ai") is None


def test_ai_questions_are_checked_saved_and_reloaded(tmp_path):
    bank = QuestionBank(curated(), cache_dir=tmp_path)
    duplicate = {
        **GOOD,
        "question": curated()[0]["question"],
        "correct_answer": "Something",
    }
    added = bank.add_ai([GOOD, GOOD, duplicate, {"junk": True}])
    assert added == 1
    saved = json.loads((tmp_path / "trivia_ai_questions.json").read_text())
    assert saved["questions"][0]["source"] == "ai"
    reloaded = QuestionBank(curated(), cache_dir=tmp_path)
    assert len(reloaded.ai) == 1 and reloaded.ai[0]["correct_answer"] == "Venus"


def test_parse_questions_tolerates_code_fences():
    text = "Here you go!\n```json\n" + json.dumps([GOOD]) + "\n```"
    assert parse_questions(text) == [GOOD]
    assert parse_questions("sorry, no") == []


class FakeLLM:
    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    async def chat_completion(self, messages, system=None, tools=None):
        self.calls += 1
        for i in range(0, len(self.reply), 50):
            yield self.reply[i : i + 50]


def test_question_maker_adds_questions_in_the_background(tmp_path):
    bank = QuestionBank(curated(), cache_dir=tmp_path)
    llm = FakeLLM(json.dumps([GOOD]))
    now = [100.0]
    maker = QuestionMaker(
        llm_source=lambda: llm, min_interval_seconds=45, clock=lambda: now[0]
    )

    async def go():
        assert maker.maybe_start(bank, 8)
        assert not maker.maybe_start(bank, 8)  # one at a time
        await maker._task
        assert not maker.maybe_start(bank, 8)  # and not again right away
        now[0] += 60
        assert maker.maybe_start(bank, 8)
        await maker._task

    asyncio.run(go())
    assert llm.calls == 2 and len(bank.ai) == 1 and maker.added == 1


def test_question_maker_is_off_without_an_llm():
    assert not QuestionMaker().maybe_start(QuestionBank(curated(), cache_dir=None), 8)


def test_starting_trivia_asks_for_questions_but_other_games_and_idle_do_not(
    monkeypatch,
):
    from open_llm_vtuber.room.live_message import LiveMessage
    from open_llm_vtuber.room.profiles import load_room
    from open_llm_vtuber.room.session import RoomSession

    session = RoomSession(load_room(ROOT / "room", ROOT))
    started = []
    monkeypatch.setattr(
        session.show.question_maker,
        "maybe_start",
        lambda bank, count: started.append(count) or True,
    )
    for tick in range(50):  # idle: nothing
        session.show.tick()
    assert started == []

    def say(text):
        session.show.observe(LiveMessage("youtube", text, "@selwyn", text, 0.0))

    say("play tic tac toe")
    assert started == []
    say("stop the game")
    say("play trivia")
    assert started == [8]
