"""Every chat message the server sees, for the operator's chat window
(/vr-agent/chat.html, also usable as an OBS custom browser dock). Never shown
on stream. Kept in memory only (the last 300)."""

from __future__ import annotations

import itertools
import time
from collections import deque
from typing import Any

_ids = itertools.count(1)
FEED: deque[dict[str, Any]] = deque(maxlen=300)


def record(author: str, text: str, status: str) -> None:
    FEED.append(
        {
            "id": next(_ids),
            "at": time.time(),
            "author": str(author or "")[:60],
            "text": str(text or "")[:300],
            "status": status,
        }
    )


def since(after: int) -> list[dict[str, Any]]:
    return [item for item in FEED if item["id"] > after]
