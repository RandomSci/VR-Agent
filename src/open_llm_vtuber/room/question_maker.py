"""AI-written trivia questions, made only when a viewer starts a trivia game.

One LLM request per started game (the LLM from conf.yaml, counted in the
usage meter like every other request) writes a few brand-new questions in the
background while the first question is already on the board. The game never
waits for it, and nothing here runs while nobody plays, so the idle stream
still makes zero requests. Every question is checked by the bank before it
can be asked; the answers stored with it are what scoring uses.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, Callable, Optional

from loguru import logger

from ..vr_agent.usage import count_llm_calls

SYSTEM = (
    "You write trivia questions for a cozy family-friendly livestream quiz. "
    "You reply with JSON only."
)


def build_prompt(
    count: int, categories: list[str], avoid: list[str], rng: random.Random
) -> str:
    picked = (
        rng.sample(categories, min(len(categories), 4)) if categories else ["general"]
    )
    avoid_lines = "\n".join(f"- {q}" for q in avoid)
    return (
        f"Write {count} NEW trivia questions for a live chat to answer by typing.\n"
        f"Use these categories (exact names): {', '.join(picked)}.\n"
        "Mix difficulties: easy, medium, hard.\n"
        "Rules:\n"
        "- Only well known, verifiable facts that will not change over time. No current events, "
        "no 'current' office holders, no records that may be broken, nothing debatable.\n"
        "- The correct answer is 1 to 3 words, easy to type in chat, and never appears in the question.\n"
        "- accepted_answers holds other correct spellings or short forms of the same answer.\n"
        "- wrong_answers holds 3 plausible but clearly wrong answers.\n"
        "- Family friendly. No politics, religion, violence or tragedies.\n"
        "- Do not repeat or rephrase any of these existing questions:\n"
        f"{avoid_lines}\n"
        'Reply with a JSON array only, like: [{"category": "space", "difficulty": "easy", '
        '"question": "Which planet is called the Red Planet?", "correct_answer": "Mars", '
        '"accepted_answers": ["planet mars"], "wrong_answers": ["Venus", "Jupiter", "Saturn"]}]'
    )


def parse_questions(text: str) -> list[dict[str, Any]]:
    """The JSON array from a model reply (tolerates code fences and chatter)."""
    if not text:
        return []
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except (ValueError, TypeError):
        return []
    return (
        [item for item in data if isinstance(item, dict)]
        if isinstance(data, list)
        else []
    )


def conf_llm_source(context_source: Callable[[], Any]) -> Callable[[], Any]:
    """Builds (once) a stateless LLM from conf.yaml's agent settings."""
    cache: dict[str, Any] = {}

    def source() -> Any:
        if "llm" in cache:
            return cache["llm"]
        from ..agent.stateless_llm_factory import LLMFactory

        context = context_source()
        agent_config = context.character_config.agent_config
        settings = agent_config.agent_settings.model_dump()
        provider = (settings.get("basic_memory_agent") or {}).get("llm_provider")
        config = dict(agent_config.llm_configs.model_dump().get(provider) or {})
        config.pop("interrupt_method", None)
        if not provider or not config:
            raise RuntimeError("no LLM configured in conf.yaml")
        llm = LLMFactory.create_llm(
            llm_provider=provider, system_prompt=SYSTEM, **config
        )
        cache["llm"] = count_llm_calls(llm, source="trivia_questions")
        return cache["llm"]

    return source


class QuestionMaker:
    def __init__(
        self,
        llm_source: Optional[Callable[[], Any]] = None,
        min_interval_seconds: float = 45.0,
        rng: Optional[random.Random] = None,
        clock=time.time,
    ):
        self.llm_source = llm_source
        self.min_interval = min_interval_seconds
        self.rng = rng or random.Random()
        self.clock = clock
        self._task: Optional[asyncio.Task] = None
        self._last_started = 0.0
        self.requests = 0
        self.added = 0
        self.last_error = ""

    def maybe_start(self, bank: Any, count: int) -> bool:
        """Kick off one background request unless one is running or ran recently."""
        if self.llm_source is None or bank is None or count <= 0:
            return False
        if self._task and not self._task.done():
            return False
        now = self.clock()
        if now - self._last_started < self.min_interval:
            return False
        self._last_started = now
        try:
            self._task = asyncio.get_running_loop().create_task(
                self.top_up(bank, count), name="vr-trivia-questions"
            )
        except RuntimeError:
            return False
        return True

    async def top_up(self, bank: Any, count: int) -> int:
        try:
            llm = self.llm_source()
            prompt = build_prompt(
                count,
                list(bank.categories),
                bank.sample_questions(40, self.rng),
                self.rng,
            )
            self.requests += 1
            parts: list[str] = []
            stream = llm.chat_completion([{"role": "user", "content": prompt}], SYSTEM)
            async for chunk in stream:
                if isinstance(chunk, str):
                    parts.append(chunk)
            added = bank.add_ai(parse_questions("".join(parts)))
            self.added += added
            logger.info(
                f"Trivia: {added} new AI questions saved ({bank.describe()['ai']} in total)"
            )
            return added
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc)[:200]
            logger.warning(f"Trivia: AI questions unavailable this time: {exc}")
            return 0

    def describe(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "added": self.added,
            "running": bool(self._task and not self._task.done()),
            "last_error": self.last_error,
        }
