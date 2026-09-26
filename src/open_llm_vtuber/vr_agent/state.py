"""VR Agent runtime state.

One small state holder shared by the chat source, the response loop and the
WebSocket handler. It records the current phase, keeps a short transition
history for developer monitoring, and pushes a minimal public state to the
connected frontends so the livestream overlay can show "Listening",
"Thinking" or "Responding". Internal details (errors, queue sizes, browser
status) stay in logs and the status route and are never broadcast.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from enum import Enum
from typing import Awaitable, Callable, Deque, Optional

from loguru import logger

from .metrics import LatencyTracker


class VRAgentState(str, Enum):
    STARTING = "STARTING"
    WAITING_FOR_STREAM = "WAITING_FOR_STREAM"
    IDLE = "IDLE"
    MESSAGE_RECEIVED = "MESSAGE_RECEIVED"
    THINKING = "THINKING"
    SPEAKING = "SPEAKING"
    PERFORMING_ACTION = "PERFORMING_ACTION"
    RECONNECTING = "RECONNECTING"
    ERROR_RECOVERABLE = "ERROR_RECOVERABLE"


# What the public overlay is allowed to know. Anything not listed maps to
# "listening" so viewers never see reconnects or errors.
_PUBLIC_PHASE = {
    VRAgentState.IDLE: "listening",
    VRAgentState.MESSAGE_RECEIVED: "thinking",
    VRAgentState.THINKING: "thinking",
    VRAgentState.SPEAKING: "responding",
    VRAgentState.PERFORMING_ACTION: "responding",
}

Broadcaster = Callable[[str], Awaitable[None]]


class VRAgentRuntime:
    """Process-wide state holder. Cheap to call from anywhere."""

    def __init__(self) -> None:
        self._state = VRAgentState.STARTING
        self._since = time.time()
        self._detail = ""
        self._history: Deque[dict] = deque(maxlen=50)
        self._broadcaster: Optional[Broadcaster] = None
        self._last_public: Optional[str] = None
        self.latency = LatencyTracker()

    @property
    def state(self) -> VRAgentState:
        return self._state

    def set_broadcaster(self, broadcaster: Optional[Broadcaster]) -> None:
        self._broadcaster = broadcaster

    def set(self, state: VRAgentState, detail: str = "") -> None:
        if state == self._state and detail == self._detail:
            return
        previous = self._state
        now = time.time()
        self._history.append(
            {
                "from": previous.value,
                "to": state.value,
                "at": now,
                "after_s": round(now - self._since, 3),
                "detail": detail[:200],
            }
        )
        self._state, self._since, self._detail = state, now, detail
        logger.debug(
            f"VR Agent state {previous.value} -> {state.value} {detail}".rstrip()
        )
        self._schedule_broadcast()

    def public_phase(self) -> str:
        return _PUBLIC_PHASE.get(self._state, "listening")

    def public_payload(self) -> str:
        return json.dumps({"type": "vr-agent-state", "phase": self.public_phase()})

    def snapshot(self) -> dict:
        return {
            "state": self._state.value,
            "since": self._since,
            "seconds_in_state": round(time.time() - self._since, 1),
            "detail": self._detail,
            "recent_transitions": list(self._history)[-10:],
        }

    def _schedule_broadcast(self) -> None:
        if not self._broadcaster:
            return
        phase = self.public_phase()
        if phase == self._last_public:
            return
        self._last_public = phase
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._safe_broadcast(self.public_payload()))

    async def _safe_broadcast(self, payload: str) -> None:
        try:
            await self._broadcaster(payload)  # type: ignore[misc]
        except Exception as exc:  # pragma: no cover - network dependent
            logger.debug(f"VR Agent state broadcast failed: {exc}")


runtime = VRAgentRuntime()
