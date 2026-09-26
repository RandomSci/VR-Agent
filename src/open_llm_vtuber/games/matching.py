"""Deterministic answer matching for chat answers. No LLM involved."""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Iterable

_NUMBER_WORDS = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "ten": "10",
    "eleven": "11",
    "twelve": "12",
    "thirteen": "13",
    "fourteen": "14",
    "fifteen": "15",
    "sixteen": "16",
    "seventeen": "17",
    "eighteen": "18",
    "nineteen": "19",
    "twenty": "20",
}
_FILLER = {
    "the",
    "a",
    "an",
    "its",
    "it",
    "is",
    "i",
    "think",
    "guess",
    "answer",
    "maybe",
    "probably",
    "that",
    "thats",
    "must",
    "be",
    "definitely",
    "obviously",
    "final",
    "hmm",
    "um",
    "uh",
    "ok",
    "okay",
    "lol",
    "yes",
    "sure",
    "of",
    "course",
}


def normalise(text: str) -> str:
    text = (
        unicodedata.normalize("NFKD", str(text or ""))
        .encode("ascii", "ignore")
        .decode()
    )
    text = text.lower().replace("'", "").replace(",", "")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    words = [_NUMBER_WORDS.get(w, w) for w in text.split()]
    return " ".join(words)


def core(text: str) -> str:
    """Normalised text without filler words ("it's the moon!" -> "moon")."""
    return " ".join(w for w in normalise(text).split() if w not in _FILLER)


def _contains_phrase(haystack: str, needle: str) -> bool:
    return (
        bool(needle)
        and re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack)
        is not None
    )


def is_correct(
    message: str,
    accepted: Iterable[str],
    wrong: Iterable[str] = (),
    typo_tolerance: float = 0.86,
) -> bool:
    """True when the message gives an accepted answer and no competing guess."""
    said = core(message)
    if not said:
        return False
    accepted_cores = [c for c in (core(a) for a in accepted) if c]
    wrong_cores = [c for c in (core(w) for w in wrong) if c]

    # Hedging between options ("mars or venus") is not an answer.
    if any(
        _contains_phrase(said, w)
        for w in wrong_cores
        if not any(w in a for a in accepted_cores)
    ):
        return False

    for answer in accepted_cores:
        if said == answer or _contains_phrase(said, answer):
            return True
        if len(answer) >= 5 and len(said) >= 5:
            if difflib.SequenceMatcher(None, said, answer).ratio() >= typo_tolerance:
                return True
    return False


def looks_like_answer(message: str, max_words: int) -> bool:
    """Short, non-question chat during an open question is an answer attempt."""
    text = str(message or "").strip()
    if not text or "?" in text:
        return False
    words = normalise(text).split()
    return 0 < len(words) <= max_words
