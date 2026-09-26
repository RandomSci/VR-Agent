"""Response latency tracking for developer monitoring (never shown on stream)."""

from __future__ import annotations

import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional


@dataclass
class ResponseTiming:
    """Timeline of one viewer message, all values are epoch seconds."""

    message_id: str
    received_at: float
    selected_at: float = 0.0
    first_audio_at: float = 0.0
    finished_at: float = 0.0
    action: Optional[str] = None

    def as_dict(self) -> dict:
        def span(a: float, b: float) -> Optional[float]:
            return round(b - a, 2) if a and b else None

        return {
            "message_id": self.message_id,
            "queue_wait_s": span(self.received_at, self.selected_at),
            "time_to_first_audio_s": span(self.selected_at, self.first_audio_at),
            "chat_to_voice_s": span(self.received_at, self.first_audio_at),
            "total_s": span(self.received_at, self.finished_at),
            "action": self.action,
        }


@dataclass
class LatencyTracker:
    window: int = 50
    _items: Deque[ResponseTiming] = field(default_factory=lambda: deque(maxlen=50))

    def start(self, message_id: str, received_at: float) -> ResponseTiming:
        timing = ResponseTiming(
            message_id=message_id, received_at=received_at, selected_at=time.time()
        )
        self._items.append(timing)
        return timing

    def summary(self) -> dict:
        done = [t.as_dict() for t in self._items]
        voice = [d["chat_to_voice_s"] for d in done if d["chat_to_voice_s"] is not None]
        first = [
            d["time_to_first_audio_s"]
            for d in done
            if d["time_to_first_audio_s"] is not None
        ]

        def stats(values: list[float]) -> Optional[dict]:
            if not values:
                return None
            ordered = sorted(values)
            p90 = ordered[min(len(ordered) - 1, int(round(0.9 * (len(ordered) - 1))))]
            return {
                "count": len(values),
                "p50": round(statistics.median(values), 2),
                "p90": round(p90, 2),
                "max": round(max(values), 2),
            }

        return {
            "chat_to_voice": stats(voice),
            "selected_to_first_audio": stats(first),
            "last": done[-1] if done else None,
        }
