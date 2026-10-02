"""Now and then, post the gallery link in live chat, without spamming.

The link only goes out when enough different viewers are chatting (an empty
chat gets nothing), never more often than every VR_PROMO_EVERY_MINUTES, and
only when something is published. Off unless YouTube chat posting is on.

    VR_GALLERY_PROMO=0           turn it off
    VR_PROMO_MIN_VIEWERS=3       distinct chatters in the last 15 minutes
    VR_PROMO_EVERY_MINUTES=20    minimum gap between two posts
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional

from loguru import logger

WINDOW_SECONDS = 15 * 60
PROMO_TEXT = (
    "🎮 Every game Mika and Luna built live is here: {url} "
    "Search your username to find yours and share it with friends!"
)


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


class GalleryPromoter:
    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        service_factory: Optional[Callable[[], object]] = None,
    ):
        self.clock = clock
        self.service_factory = service_factory
        self.seen: dict[str, float] = {}  # author -> last message time
        self.last_post = 0.0
        self.started = clock()
        self._busy = threading.Lock()

    @staticmethod
    def enabled() -> bool:
        return os.environ.get("VR_GALLERY_PROMO", "1").strip().lower() not in (
            "0",
            "false",
            "no",
        )

    def note(self, author: str) -> bool:
        """Record one viewer message. True when a promo post is due now."""
        now = self.clock()
        key = str(author or "").strip().lower()
        if key:
            self.seen[key] = now
        for name, at in list(self.seen.items()):
            if now - at > WINDOW_SECONDS:
                self.seen.pop(name, None)
        if not self.enabled():
            return False
        every = _int_env("VR_PROMO_EVERY_MINUTES", 20) * 60
        # Not in the first minutes of a stream, and never twice within the gap.
        if now - max(self.last_post, self.started) < min(every, 10 * 60) or (
            self.last_post and now - self.last_post < every
        ):
            return False
        return len(self.seen) >= _int_env("VR_PROMO_MIN_VIEWERS", 3)

    def post(self) -> dict:
        """Post once (runs in a worker thread). Marks the time even on failure,
        so a broken setup is not retried every message."""
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "reason": "already posting"}
        try:
            self.last_post = self.clock()
            if self.service_factory is not None:
                service = self.service_factory()
            else:
                from .service import PublicationService
                from .settings import PublishSettings

                service = PublicationService(PublishSettings.from_env())
            result = service.post_gallery_promo(PROMO_TEXT)
            logger.info(
                f"Gallery link in chat: {'posted' if result.get('ok') else result.get('reason')}"
            )
            return result
        except Exception as exc:  # promotion must never disturb the stream
            logger.warning(f"Gallery promo failed: {exc}")
            return {"ok": False, "reason": str(exc)[:200]}
        finally:
            self._busy.release()


promoter = GalleryPromoter()
