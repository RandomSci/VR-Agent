"""Counters for paid API usage: LLM requests and TTS requests.

Every LLM request and every TTS generation in the server passes through one
of the ``record_*`` calls below, so these numbers are the ground truth for
"no chat means no spending". They are exposed at /vr-agent/status and
/vr-agent/room/status and checked by the zero-activity tests.
"""

from __future__ import annotations

import threading
import time
from collections import Counter
from typing import Any, Optional


class UsageMeter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "_lock", threading.Lock()):
            self.llm_requests = 0
            self.tts_requests = 0
            self.viewer_triggered_interactions = 0
            self.last_llm_at = 0.0
            self.last_tts_at = 0.0
            self.last_viewer_interaction_at = 0.0
            self.llm_by_source: Counter[str] = Counter()
            self.tts_by_source: Counter[str] = Counter()
            self.started_at = time.time()

    def record_llm(self, source: Optional[str] = None) -> None:
        with self._lock:
            self.llm_requests += 1
            self.last_llm_at = time.time()
            self.llm_by_source[source or "unknown"] += 1

    def record_tts(self, source: Optional[str] = None) -> None:
        with self._lock:
            self.tts_requests += 1
            self.last_tts_at = time.time()
            self.tts_by_source[source or "unknown"] += 1

    def record_viewer_interaction(self) -> None:
        with self._lock:
            self.viewer_triggered_interactions += 1
            self.last_viewer_interaction_at = time.time()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "llm_requests": self.llm_requests,
                "tts_requests": self.tts_requests,
                "viewer_triggered_interactions": self.viewer_triggered_interactions,
                "llm_by_source": dict(self.llm_by_source),
                "tts_by_source": dict(self.tts_by_source),
                "last_llm_at": self.last_llm_at or None,
                "last_tts_at": self.last_tts_at or None,
                "last_viewer_interaction_at": self.last_viewer_interaction_at or None,
                "since": self.started_at,
            }


usage = UsageMeter()


def count_llm_calls(llm: Any, source: str = "agent") -> Any:
    """Wrap an LLM instance's ``chat_completion`` so each call is counted.

    The instance keeps its class (``isinstance`` checks still work); only the
    bound method is replaced. Safe to call twice.
    """
    original = getattr(llm, "chat_completion", None)
    if original is None or getattr(original, "_vr_counted", False):
        return llm

    def chat_completion(*args, **kwargs):
        from . import latency_trace

        usage.record_llm(getattr(llm, "_vr_usage_source", None) or source)
        latency_trace.mark("llm_request_started")
        result = original(*args, **kwargs)
        if hasattr(result, "__aiter__"):
            return _first_token_marker(result)
        return result

    chat_completion._vr_counted = True  # type: ignore[attr-defined]
    try:
        llm.chat_completion = chat_completion
    except Exception:  # pragma: no cover - exotic LLM objects
        return llm
    return llm


async def _first_token_marker(stream: Any):
    """Pass an LLM token stream through, marking the first token's arrival."""
    from . import latency_trace

    first = True
    async for item in stream:
        if first:
            latency_trace.mark("llm_first_token")
            first = False
        yield item
