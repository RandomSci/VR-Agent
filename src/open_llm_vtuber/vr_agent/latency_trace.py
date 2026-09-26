"""End-to-end latency trace for one viewer interaction.

Every stage from the chat message appearing in the YouTube page to the first
audible speech gets a timestamp. Code deep in the pipeline (LLM wrapper, TTS
manager, TTS engines) marks the *current* trace through a context variable,
so nothing has to be passed through every call. asyncio tasks and
``asyncio.to_thread`` copy the context, so TTS threads mark the right trace.

Times are wall-clock seconds derived from a monotonic high-resolution clock
(``perf_counter``), anchored once per trace, so deltas are precise and
browser timestamps (``Date.now()``) can still be placed on the same axis.
"""

from __future__ import annotations

import contextvars
import statistics
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

STAGES = (
    "chat_dom_inserted",
    "chat_detected",
    "chat_filtered",
    "chat_selected",
    "routing_started",
    "routing_finished",
    "llm_request_started",
    "llm_first_token",
    "first_speakable_chunk_ready",
    "tts_request_started",
    "tts_first_byte",
    "tts_first_audio_received",
    "audio_payload_sent",
    "audio_playback_started",
    "interaction_finished",
)

# (label, from stage, to stage) for the report
SPANS = (
    ("dom_to_detection", "chat_dom_inserted", "chat_detected"),
    ("filter", "chat_detected", "chat_filtered"),
    ("queue_and_selection", "chat_filtered", "chat_selected"),
    ("selection_to_routing", "chat_selected", "routing_started"),
    ("character_routing", "routing_started", "routing_finished"),
    ("routing_to_llm_request", "routing_finished", "llm_request_started"),
    ("llm_first_token", "llm_request_started", "llm_first_token"),
    ("first_speakable_chunk", "llm_first_token", "first_speakable_chunk_ready"),
    ("chunk_to_tts_request", "first_speakable_chunk_ready", "tts_request_started"),
    ("tts_first_byte", "tts_request_started", "tts_first_byte"),
    ("tts_first_audio", "tts_request_started", "tts_first_audio_received"),
    ("audio_prepare_and_send", "tts_first_audio_received", "audio_payload_sent"),
    ("playback_scheduling", "audio_payload_sent", "audio_playback_started"),
    ("total_detected_to_audible", "chat_detected", "audio_playback_started"),
    ("total_dom_to_audible", "chat_dom_inserted", "audio_playback_started"),
)

_current: contextvars.ContextVar[Optional["LatencyTrace"]] = contextvars.ContextVar(
    "vr_latency_trace", default=None
)


@dataclass
class LatencyTrace:
    key: str
    marks: dict[str, float] = field(default_factory=dict)
    info: dict[str, Any] = field(default_factory=dict)
    _wall0: float = field(default_factory=time.time)
    _perf0: float = field(default_factory=time.perf_counter)

    def now(self) -> float:
        return self._wall0 + (time.perf_counter() - self._perf0)

    def mark(self, stage: str, at: Optional[float] = None, once: bool = True) -> None:
        if once and stage in self.marks:
            return
        self.marks[stage] = self.now() if at is None else float(at)

    def span_ms(self, start: str, end: str) -> Optional[float]:
        if start in self.marks and end in self.marks:
            return round((self.marks[end] - self.marks[start]) * 1000, 1)
        return None

    def report(self) -> dict[str, Any]:
        return {
            "key": self.key,
            **self.info,
            "spans_ms": {label: self.span_ms(a, b) for label, a, b in SPANS},
            "stages": [s for s in STAGES if s in self.marks],
        }


class LatencyTracker:
    """Bounded store of recent traces, keyed by chat message id."""

    def __init__(self, keep: int = 60):
        self.keep = keep
        self._traces: "OrderedDict[str, LatencyTrace]" = OrderedDict()
        self._by_client: dict[str, LatencyTrace] = {}

    def start(self, key: str) -> LatencyTrace:
        trace = self._traces.get(key)
        if trace is None:
            trace = LatencyTrace(key)
            self._traces[key] = trace
            while len(self._traces) > self.keep:
                self._traces.popitem(last=False)
        return trace

    def get(self, key: str) -> Optional[LatencyTrace]:
        return self._traces.get(key)

    def set_client_trace(self, client_uid: str, trace: Optional[LatencyTrace]) -> None:
        if trace is None:
            self._by_client.pop(client_uid, None)
        else:
            self._by_client[client_uid] = trace

    def client_trace(self, client_uid: str) -> Optional[LatencyTrace]:
        return self._by_client.get(client_uid)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        done = [t for t in self._traces.values() if "audio_playback_started" in t.marks]
        return [t.report() for t in done[-limit:]]

    def summary(self) -> dict[str, Any]:
        done = [t for t in self._traces.values() if "audio_playback_started" in t.marks]
        out: dict[str, Any] = {"interactions": len(done), "spans_ms": {}}
        for label, a, b in SPANS:
            values = [v for v in (t.span_ms(a, b) for t in done) if v is not None]
            if values:
                ordered = sorted(values)
                out["spans_ms"][label] = {
                    "p50": round(statistics.median(values), 1),
                    "p90": ordered[
                        min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))
                    ],
                    "last": values[-1],
                    "count": len(values),
                }
        if done:
            last = done[-1]
            bottleneck = max(
                (
                    (label, last.span_ms(a, b) or 0.0)
                    for label, a, b in SPANS
                    if not label.startswith("total") and label != "tts_first_byte"
                ),
                key=lambda item: item[1],
            )
            out["largest_stage_last"] = {"stage": bottleneck[0], "ms": bottleneck[1]}
        return out


tracker = LatencyTracker()


# ---------------------------------------------------------------------------
# context helpers used deep in the pipeline
# ---------------------------------------------------------------------------
def current() -> Optional[LatencyTrace]:
    return _current.get()


def activate(trace: Optional[LatencyTrace]) -> contextvars.Token:
    return _current.set(trace)


def deactivate(token: contextvars.Token) -> None:
    try:
        _current.reset(token)
    except ValueError:
        _current.set(None)


def mark(stage: str, once: bool = True) -> None:
    """Mark a stage on the interaction currently running (no-op outside one)."""
    trace = _current.get()
    if trace is not None:
        trace.mark(stage, once=once)
