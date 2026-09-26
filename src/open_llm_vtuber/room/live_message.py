"""Platform-neutral live chat messages.

Every chat source (YouTube today, TikTok or Twitch later) normalises its
messages into ``LiveMessage`` before they reach the room, the Conversation
Director or the Game Engine, so none of those care where a message came from.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from ..vr_agent.text_safety import clean_viewer_text


@dataclass(frozen=True)
class LiveMessage:
    platform: str
    message_id: str
    username: str
    text: str
    timestamp: float
    author_id: str = ""
    kind: str = "text"  # "text" or "paid"
    amount: str = ""
    author_type: str = ""  # "", "member", "moderator", "owner"
    dom_at: float = 0.0  # when the message appeared in the YouTube page (epoch s)
    detected_at: float = 0.0  # when our chat reader picked it up (epoch s)

    @property
    def display_name(self) -> str:
        return clean_viewer_text(self.username, 60) or "viewer"

    @property
    def clean_text(self) -> str:
        return clean_viewer_text(self.text, 280)

    @property
    def is_system(self) -> bool:
        return self.author_id.startswith("system-")

    @classmethod
    def from_youtube(cls, message: Any) -> "LiveMessage":
        stamp = getattr(message, "timestamp", None)
        return cls(
            platform="youtube",
            message_id=str(getattr(message, "message_id", "")),
            username=str(getattr(message, "author_display_name", "") or "viewer"),
            text=str(getattr(message, "text", "")),
            timestamp=stamp.timestamp() if hasattr(stamp, "timestamp") else time.time(),
            author_id=str(getattr(message, "author_channel_id", "")),
            kind=str(getattr(message, "kind", "text")),
            amount=str(getattr(message, "amount", "")),
            author_type=str(getattr(message, "author_type", "")),
            dom_at=float(getattr(message, "dom_at", 0.0) or 0.0),
            detected_at=float(getattr(message, "detected_at", 0.0) or 0.0),
        )


class LiveChatSource(Protocol):
    """What a chat source provides. YouTubePlaywrightChatSource fits this shape."""

    def ready(self) -> tuple[bool, str]: ...

    async def run(self, on_messages) -> None: ...

    async def stop(self) -> None: ...
