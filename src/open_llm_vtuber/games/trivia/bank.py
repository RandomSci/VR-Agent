"""Trivia question bank: the curated questions plus AI-written extras, and a
saved rotation so questions do not repeat until the whole pool was asked.

Pure data and files. The AI questions are written by the room's question
maker (an LLM call made only when a viewer starts a trivia game) and checked
here before they can be asked. Scoring never uses an LLM: answers are
matched against the accepted answers stored with each question.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from loguru import logger

DIFFICULTIES = ("easy", "medium", "hard")
DEFAULT_CACHE_DIR = Path("cache")


def normalise_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def clean_question(
    raw: dict[str, Any], categories: Iterable[str], source: str
) -> Optional[dict[str, Any]]:
    """A valid question dict, or None. Used for AI questions before they are kept."""
    if not isinstance(raw, dict):
        return None
    question = " ".join(str(raw.get("question") or "").split())
    correct = " ".join(str(raw.get("correct_answer") or "").split())
    difficulty = str(raw.get("difficulty") or "medium").lower().strip()
    category = str(raw.get("category") or "general").lower().strip()
    accepted = [
        " ".join(str(a).split())
        for a in raw.get("accepted_answers") or []
        if str(a).strip()
    ]
    wrong = [
        " ".join(str(w).split())
        for w in raw.get("wrong_answers") or []
        if str(w).strip()
    ]
    allowed = set(categories)
    if not (12 <= len(question) <= 160) or not question.endswith("?"):
        return None
    if not correct or len(correct) > 40 or len(correct.split()) > 4:
        return None  # viewers must be able to type it quickly
    if difficulty not in DIFFICULTIES or (allowed and category not in allowed):
        return None
    n_correct = normalise_text(correct)
    if not n_correct or re.search(
        rf"\b{re.escape(n_correct)}\b", normalise_text(question)
    ):
        return None  # the question gives the answer away
    wrong = [w for w in wrong if normalise_text(w) and normalise_text(w) != n_correct][
        :3
    ]
    if len(wrong) < 2 or len({normalise_text(w) for w in wrong}) < len(wrong):
        return None
    accepted = [correct] + [
        a for a in accepted if normalise_text(a) != n_correct and len(a) <= 40
    ][:4]
    if any(normalise_text(a) in {normalise_text(w) for w in wrong} for a in accepted):
        return None
    if re.search(r"https?://|<|>|\{|\}", question + correct + " ".join(wrong)):
        return None
    return {
        "question": question,
        "correct_answer": correct,
        "accepted_answers": accepted,
        "wrong_answers": wrong,
        "difficulty": difficulty,
        "category": category,
        "source": source,
    }


class QuestionBank:
    """Curated questions (read only) plus AI questions (saved in cache/)."""

    def __init__(
        self,
        curated: list[dict[str, Any]],
        cache_dir: Optional[Path] = DEFAULT_CACHE_DIR,
        max_ai_questions: int = 1500,
        remember: int = 5000,
    ):
        self.curated = list(curated)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.max_ai_questions = max_ai_questions
        self.remember = remember
        self.ai: list[dict[str, Any]] = []
        self.seen: dict[str, float] = {}  # question id -> last time asked
        self._lock = threading.Lock()
        self.categories = tuple(sorted({q["category"] for q in self.curated}))
        self._load()

    # ------------------------------------------------------------------
    @property
    def ai_path(self) -> Optional[Path]:
        return self.cache_dir / "trivia_ai_questions.json" if self.cache_dir else None

    @property
    def seen_path(self) -> Optional[Path]:
        return self.cache_dir / "trivia_seen.json" if self.cache_dir else None

    def _load(self) -> None:
        try:
            if self.ai_path and self.ai_path.is_file():
                data = json.loads(self.ai_path.read_text(encoding="utf-8"))
                for raw in data.get("questions", []):
                    q = clean_question(raw, self.categories, "ai")
                    if q and raw.get("id"):
                        q["id"] = str(raw["id"])[:24]
                        self.ai.append(q)
        except Exception as exc:
            logger.warning(f"Trivia: AI question cache unreadable, ignoring it: {exc}")
        try:
            if self.seen_path and self.seen_path.is_file():
                data = json.loads(self.seen_path.read_text(encoding="utf-8"))
                self.seen = {
                    str(k): float(v) for k, v in (data.get("seen") or {}).items()
                }
        except Exception as exc:
            logger.warning(
                f"Trivia: question history unreadable, starting fresh: {exc}"
            )

    def _write(self, path: Optional[Path], payload: dict[str, Any]) -> None:
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(path)
        except Exception as exc:
            logger.warning(f"Trivia: could not save {path.name}: {exc}")

    # ------------------------------------------------------------------
    def all(self) -> list[dict[str, Any]]:
        return self.curated + self.ai

    def unseen_count(self) -> int:
        return sum(1 for q in self.all() if q["id"] not in self.seen)

    def mark_asked(self, question_id: str, now: Optional[float] = None) -> None:
        with self._lock:
            self.seen[question_id] = time.time() if now is None else now
            if len(self.seen) > self.remember:
                for key, _ in sorted(self.seen.items(), key=lambda kv: kv[1])[
                    : len(self.seen) - self.remember
                ]:
                    self.seen.pop(key, None)
            snapshot = dict(self.seen)
        self._write(self.seen_path, {"seen": snapshot})

    def pick(self, candidates: list[dict[str, Any]], rng) -> dict[str, Any]:
        """Never asked (in any stream) first; otherwise the one asked longest ago."""
        fresh = [q for q in candidates if q["id"] not in self.seen]
        if fresh:
            return rng.choice(fresh)
        oldest = min(self.seen.get(q["id"], 0.0) for q in candidates)
        pool = [q for q in candidates if self.seen.get(q["id"], 0.0) <= oldest + 1.0]
        return rng.choice(pool)

    def known_questions(self) -> set[str]:
        return {normalise_text(q["question"]) for q in self.all()}

    def add_ai(self, raw_questions: Iterable[dict[str, Any]]) -> int:
        """Validate, de-duplicate and keep AI questions. Returns how many were added."""
        known = self.known_questions()
        known_answers = {
            (normalise_text(q["question"])[:40], normalise_text(q["correct_answer"]))
            for q in self.all()
        }
        added = 0
        with self._lock:
            for raw in raw_questions:
                q = clean_question(raw, self.categories, "ai")
                if not q:
                    continue
                key = normalise_text(q["question"])
                if (
                    key in known
                    or (key[:40], normalise_text(q["correct_answer"])) in known_answers
                ):
                    continue
                q["id"] = f"ai{int(time.time() * 1000) % 10**10:010d}{added:02d}"
                self.ai.append(q)
                known.add(key)
                added += 1
            if len(self.ai) > self.max_ai_questions:
                self.ai = self.ai[-self.max_ai_questions :]
            snapshot = [dict(q) for q in self.ai]
        if added:
            self._write(self.ai_path, {"version": 1, "questions": snapshot})
        return added

    def sample_questions(self, count: int, rng) -> list[str]:
        """A few existing questions to show the model what to avoid repeating."""
        pool = self.all()
        return [q["question"] for q in rng.sample(pool, min(count, len(pool)))]

    def describe(self) -> dict[str, Any]:
        return {
            "curated": len(self.curated),
            "ai": len(self.ai),
            "total": len(self.curated) + len(self.ai),
            "never_asked": self.unseen_count(),
            "categories": list(self.categories),
        }
