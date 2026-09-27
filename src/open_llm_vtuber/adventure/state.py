"""Authoritative adventure state and its small JSON persistence.

The state is compact on purpose: counters, ids, a few bounded histories. It is
saved atomically (write a temp file, then rename) to the VR Agent cache folder,
so stopping the stream and starting it again continues the same journey.
A missing or corrupt file never stops the stream: the adventure starts fresh
and the broken file is kept aside for inspection.
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Deque, Optional

from loguru import logger

STATE_VERSION = 1
MAX_FLAGS = 64
MAX_DISCOVERED = 48
MAX_RECENT = 12
MAX_DIALOGUE_HISTORY = 48
MOOD_KEYS = ("annoyance", "confidence", "competitive")


@dataclass
class AdventureState:
    version: int = STATE_VERSION
    adventure_id: str = ""
    run: int = 0
    seed: str = "vr-agent"
    decisions: int = 0

    region_index: int = 0
    objective_index: int = 0
    objective_distance: float = 0.0
    phase: str = "travel"  # travel | event | rest | transition | arrived
    activity: str = "setting off"

    weather: str = "clear"
    weather_since: float = 0.0  # active minutes

    active_minutes: float = 0.0  # running (not paused) time, across sessions
    travel_minutes: float = 0.0
    region_started: float = 0.0
    last_rest: float = 0.0

    flags: list[str] = field(default_factory=list)
    discovered: list[str] = field(default_factory=list)
    recent: Deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=MAX_RECENT))
    event_last: dict[str, float] = field(default_factory=dict)
    region_counts: dict[str, int] = field(default_factory=dict)
    once_done: list[str] = field(default_factory=list)
    dialogue_last: dict[str, float] = field(default_factory=dict)
    dialogue_history: Deque[str] = field(
        default_factory=lambda: deque(maxlen=MAX_DIALOGUE_HISTORY)
    )
    mood: dict[str, float] = field(default_factory=dict)
    last_game: str = ""
    completed_runs: int = 0
    saved_at: float = 0.0

    # ------------------------------------------------------------------
    def add_flag(self, flag: str) -> None:
        if flag not in self.flags:
            self.flags.append(flag)
            del self.flags[:-MAX_FLAGS]

    def remove_flag(self, flag: str) -> None:
        if flag in self.flags:
            self.flags.remove(flag)

    def discover(self, item: str) -> bool:
        if item in self.discovered:
            return False
        self.discovered.append(item)
        del self.discovered[:-MAX_DISCOVERED]
        return True

    def remember(self, event_id: str, note: str = "") -> None:
        self.recent.append(
            {"id": event_id, "note": note, "at": round(self.active_minutes, 2)}
        )

    def recent_ids(self, within_minutes: float = 30.0) -> tuple[str, ...]:
        cutoff = self.active_minutes - within_minutes
        return tuple(e["id"] for e in self.recent if e.get("at", 0) >= cutoff)

    def change_mood(self, key: str, delta: float) -> None:
        value = self.mood.get(key, 0.0) + float(delta)
        self.mood[key] = round(max(0.0, min(5.0, value)), 2)

    def decay_moods(self, minutes: float) -> None:
        """Moods drift back to calm (about one step every 20 minutes)."""
        step = minutes / 20.0
        for key, value in list(self.mood.items()):
            if value > 0:
                self.mood[key] = round(max(0.0, value - step), 3)

    # ------------------------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["recent"] = list(self.recent)
        data["dialogue_history"] = list(self.dialogue_history)
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "AdventureState":
        state = cls()
        for key, value in data.items():
            if not hasattr(state, key):
                continue
            if key == "recent":
                state.recent = deque(
                    [e for e in value if isinstance(e, dict)][-MAX_RECENT:],
                    maxlen=MAX_RECENT,
                )
            elif key == "dialogue_history":
                state.dialogue_history = deque(
                    [str(v) for v in value][-MAX_DIALOGUE_HISTORY:],
                    maxlen=MAX_DIALOGUE_HISTORY,
                )
            else:
                setattr(state, key, value)
        state.flags = [str(f) for f in state.flags][-MAX_FLAGS:]
        state.discovered = [str(d) for d in state.discovered][-MAX_DISCOVERED:]
        return state

    def prune(self, event_ids: set[str], exchange_ids: set[str]) -> None:
        """Forget ids that no longer exist in the content (keeps state bounded)."""
        self.event_last = {k: v for k, v in self.event_last.items() if k in event_ids}
        self.region_counts = {
            k: v for k, v in self.region_counts.items() if k in event_ids
        }
        self.once_done = [e for e in self.once_done if e in event_ids]
        self.dialogue_last = {
            k: v for k, v in self.dialogue_last.items() if k in exchange_ids
        }
        self.dialogue_history = deque(
            [d for d in self.dialogue_history if d in exchange_ids],
            maxlen=MAX_DIALOGUE_HISTORY,
        )


class StateStore:
    """Atomic JSON file persistence. Every failure is logged, never raised."""

    def __init__(self, path: Optional[Path | str]):
        self.path = Path(path) if path else None
        self.saves = 0
        self.last_error = ""

    @classmethod
    def default(cls) -> "StateStore":
        folder = os.environ.get("VR_AGENT_CACHE_DIR") or "cache"
        return cls(Path(folder) / "adventure_state.json")

    def load(self) -> Optional[AdventureState]:
        if not self.path or not self.path.is_file():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
                raise ValueError("unknown state version")
            return AdventureState.from_json(data)
        except Exception as exc:
            self.last_error = f"could not read state: {exc}"
            logger.warning(f"Adventure: {self.last_error}; starting fresh")
            try:
                self.path.replace(self.path.with_suffix(f".corrupt-{int(time.time())}"))
            except OSError:
                pass
            return None

    def save(self, state: AdventureState) -> bool:
        if not self.path:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            state.saved_at = time.time()
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state.to_json(), separators=(",", ":")), "utf-8")
            os.replace(tmp, self.path)
            self.saves += 1
            return True
        except Exception as exc:
            self.last_error = f"could not save state: {exc}"
            logger.warning(f"Adventure: {self.last_error}")
            return False
