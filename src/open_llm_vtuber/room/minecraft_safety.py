"""Keeps hateful or sexual chat off the stream (a YouTube strike ends a channel).

Two layers:
* a local check, instant, on every comment and name (slurs and their common
  spellings, sexual and self-harm phrases);
* OpenAI's moderation endpoint (free), on what could be spoken or built: a
  comment before it is answered, a build request before it is designed, a
  bot line before it is spoken. It waits at most 2 s; when it is down the
  local check still applies.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections import OrderedDict
from typing import Any, Optional

from loguru import logger

# Patterns, not a word list: they also catch "n1gg", "f4g", spaced letters.
LOCAL_BAD = re.compile(
    r"n\W*[i1!|]\W*g\W*g|\bn\W*[i1!]\W*g\W*(?:a|er|uh)\b|f\W*[a4@]\W*g\W*g?\W*(?:o|0)?\W*t|\bfags?\b|"
    r"\bretard|\bchink|\bspic\b|\bkike|\btrann(?:y|ie)|\bcoon\b|\bwetback|\bgook\b|"
    r"kill\W*(?:your|ur)\W*self|\bkys\b|suicide|\bnazi|\bhitler|\bheil\b|\bkkk\b|\brape|\bporn|\bonlyfans|"
    r"\bnudes?\b|\bsex\b|\bpenis|\bvagina|\bdick\b|\bcock\b|\bpussy|\bboobs?\b|\btits?\b|\bcum\b|\bhorny",
    re.I,
)
CACHE_SIZE = 2000
TIMEOUT = 2.0


def locally_bad(text: str) -> bool:
    return bool(LOCAL_BAD.search(text or ""))


class Moderator:
    def __init__(self) -> None:
        self._cache: OrderedDict[str, bool] = OrderedDict()
        self._client: Optional[Any] = None
        self._down_until = 0.0

    def _llm(self) -> Optional[Any]:
        key = (os.environ.get("OPENAI_API_KEY") or "").strip()
        if not key or os.environ.get("VR_MODERATION", "1").strip().lower() in ("0", "false", "off", "no"):
            return None
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=key, timeout=TIMEOUT, max_retries=0)
        return self._client

    async def flagged(self, text: str) -> bool:
        """True when this must not be spoken, shown or built."""
        text = " ".join(str(text or "").split())[:1000]
        if not text:
            return False
        if locally_bad(text):
            return True
        if text in self._cache:
            self._cache.move_to_end(text)
            return self._cache[text]
        llm = self._llm()
        loop = asyncio.get_running_loop()
        if llm is None or loop.time() < self._down_until:
            return False
        try:
            result = await asyncio.wait_for(
                llm.moderations.create(model="omni-moderation-latest", input=text), timeout=TIMEOUT + 0.5
            )
            item = result.results[0]
            cats = item.categories
            bad = bool(
                getattr(cats, "hate", False) or getattr(cats, "hate_threatening", False)
                or getattr(cats, "harassment_threatening", False) or getattr(cats, "sexual", False)
                or getattr(cats, "sexual_minors", False) or getattr(cats, "self_harm", False)
                or getattr(cats, "self_harm_instructions", False) or getattr(cats, "violence_graphic", False)
            )
        except Exception as exc:  # down or slow: the local check stays, try again in a minute
            logger.debug(f"Moderation unavailable: {exc}")
            self._down_until = loop.time() + 60
            return False
        self._cache[text] = bad
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        return bad


MODERATOR = Moderator()
