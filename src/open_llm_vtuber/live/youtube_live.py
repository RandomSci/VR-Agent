import asyncio
import html
import json
import os
import re
import time
from collections import Counter, OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Deque, Optional

import httpx
from loguru import logger

from ..vr_agent.state import VRAgentState, runtime
from ..room.class_mode import class_mode_enabled
from . import chat_feed


YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
STARTUP_BACKLOG_GRACE_SECONDS = 30
MAX_RESPONSE_WAIT_SECONDS = 8.0

DIRECT_TO_CHARACTER_RE = re.compile(
    r"\b(you|your|yours|do you|can you|could you|would you|what do you|"
    r"tell me|how do you|are you|will you|what's your|what is your)\b",
    re.IGNORECASE,
)
SIDE_CONVERSATION_RE = re.compile(
    r"(^@\S+|\b(chat|guys|everyone|anyone|somebody|viewers|mods)\b|"
    r"\b(lol|lmao|haha|same|true|yeah|yep|nope|bruh)\b$)",
    re.IGNORECASE,
)


class YouTubeLiveError(Exception):
    """Base exception for YouTube live-chat integration."""


class YouTubeLiveChatEnded(YouTubeLiveError):
    """Raised when YouTube reports that the live chat has ended."""


@dataclass(frozen=True)
class YouTubeChatMessage:
    message_id: str
    author_channel_id: str
    author_display_name: str
    text: str
    timestamp: datetime
    kind: str = "text"  # "text" or "paid" (Super Chat)
    amount: str = ""
    author_type: str = ""  # "", "member", "moderator", "owner"

    @property
    def age_seconds(self) -> float:
        return max(0.0, (datetime.now(timezone.utc) - self.timestamp).total_seconds())


@dataclass(frozen=True)
class SelectionResult:
    selected_message_id: Optional[str]
    reason: str
    confidence: float


QUIZ_ANSWER_RE = re.compile(r"\s*\(?[abcdABCD]\)?[.!]?\s*")


def _truncate(text: str, limit: int = 140) -> str:
    return text if len(text) <= limit else f"{text[:limit]}..."


def _normalize_message(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    normalized = re.sub(r"(.)\1{4,}", r"\1\1", normalized)
    return normalized


def _message_priority_score(message: YouTubeChatMessage) -> float:
    text = message.text.strip()
    normalized = _normalize_message(text)
    score = 0.0

    if message.kind == "paid":
        score += 2.5
    if "?" in text:
        score += 3.0
    if DIRECT_TO_CHARACTER_RE.search(normalized):
        score += 3.0
    if re.search(r"\b(why|how|what|when|where|can|should|would|who)\b", normalized):
        score += 1.0
    if re.search(r"\b(mili|mao|vtuber|ai)\b", normalized):
        score += 1.0
    if 12 <= len(text) <= 180:
        score += 0.7
    if len(text) < 4:
        score -= 1.5
    if SIDE_CONVERSATION_RE.search(normalized) and "?" not in text:
        score -= 2.5
    if re.match(r"^@\S+", normalized) and not DIRECT_TO_CHARACTER_RE.search(normalized):
        score -= 2.0

    return score


class YouTubeLiveChatClient:
    def __init__(
        self,
        api_key: str,
        channel_id: str,
        video_id: Optional[str] = None,
        prefer_stream_list: bool = True,
    ):
        self.api_key = api_key
        self.channel_id = channel_id
        self.video_id = video_id
        self.prefer_stream_list = prefer_stream_list
        self.next_page_token: Optional[str] = None
        self._stream_list_failed = False

    def ready(self) -> bool:
        return bool(
            self.api_key
            and not self.api_key.startswith("${")
            and (self.channel_id or self.video_id)
        )

    async def discover_active_live_chat_id(self) -> Optional[str]:
        if self.video_id:
            live_chat_id = await self._get_live_chat_id_from_video(self.video_id)
            if live_chat_id:
                logger.info("YouTube livestream detected from configured video_id.")
            return live_chat_id

        video_ids = await self._search_active_live_video_ids()
        if not video_ids:
            return None

        for video_id in video_ids:
            live_chat_id = await self._get_live_chat_id_from_video(video_id)
            if live_chat_id:
                self.video_id = video_id
                logger.info(f"YouTube livestream detected: video_id={video_id}")
                return live_chat_id
        return None

    async def _search_active_live_video_ids(self) -> list[str]:
        if not self.channel_id:
            return []
        data = await self._request_json(
            f"{YOUTUBE_API_BASE}/search",
            {
                "part": "snippet",
                "channelId": self.channel_id,
                "eventType": "live",
                "type": "video",
                "maxResults": 5,
                "key": self.api_key,
            },
        )
        items = data.get("items", [])
        return [
            item.get("id", {}).get("videoId")
            for item in items
            if item.get("id", {}).get("videoId")
        ]

    async def _get_live_chat_id_from_video(self, video_id: str) -> Optional[str]:
        data = await self._request_json(
            f"{YOUTUBE_API_BASE}/videos",
            {
                "part": "liveStreamingDetails,snippet",
                "id": video_id,
                "key": self.api_key,
            },
        )
        for item in data.get("items", []):
            live_details = item.get("liveStreamingDetails", {})
            live_chat_id = live_details.get("activeLiveChatId")
            if live_chat_id:
                return live_chat_id
        return None

    async def iter_messages(
        self, live_chat_id: str
    ) -> AsyncIterator[list[YouTubeChatMessage]]:
        while True:
            use_stream = self.prefer_stream_list and not self._stream_list_failed
            url = (
                f"{YOUTUBE_API_BASE}/liveChat/messages/streamList"
                if use_stream
                else f"{YOUTUBE_API_BASE}/liveChat/messages"
            )

            params = {
                "liveChatId": live_chat_id,
                "part": "id,snippet,authorDetails",
                "maxResults": 200,
                "key": self.api_key,
            }
            if self.next_page_token:
                params["pageToken"] = self.next_page_token

            try:
                data = await self._request_json(url, params, timeout=75.0)
            except YouTubeLiveError as exc:
                if use_stream:
                    logger.warning(
                        f"YouTube streamList unavailable; falling back to list: {exc}"
                    )
                    self._stream_list_failed = True
                    continue
                raise

            if data.get("offlineAt"):
                raise YouTubeLiveChatEnded("YouTube live chat went offline.")

            self.next_page_token = data.get("nextPageToken") or self.next_page_token
            messages = [self._parse_message(item) for item in data.get("items", [])]
            yield [message for message in messages if message]

            polling_ms = data.get("pollingIntervalMillis")
            if polling_ms is None:
                polling_ms = 1000 if use_stream else 5000
            await asyncio.sleep(max(1.0, polling_ms / 1000.0))

    async def _request_json(
        self, url: str, params: dict[str, Any], timeout: float = 20.0
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise YouTubeLiveError(f"network error: {exc}") from exc

        if response.status_code >= 400:
            reason = self._extract_error_reason(response)
            if reason in {"liveChatEnded", "liveChatNotFound"}:
                raise YouTubeLiveChatEnded(reason)
            raise YouTubeLiveError(f"HTTP {response.status_code}: {reason}")

        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise YouTubeLiveError("malformed JSON response") from exc

    @staticmethod
    def _extract_error_reason(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except Exception:
            return response.text[:120]
        errors = payload.get("error", {}).get("errors", [])
        if errors:
            return errors[0].get("reason") or errors[0].get("message") or "unknown"
        return payload.get("error", {}).get("message", "unknown")

    @staticmethod
    def _parse_message(item: dict[str, Any]) -> Optional[YouTubeChatMessage]:
        snippet = item.get("snippet", {})
        message_type = snippet.get("type")
        if message_type and message_type != "textMessageEvent":
            return None

        text = snippet.get("displayMessage") or snippet.get("textMessageDetails", {}).get(
            "messageText", ""
        )
        text = html.unescape(str(text)).strip()
        if not text:
            return None

        timestamp_raw = snippet.get("publishedAt")
        try:
            timestamp = datetime.fromisoformat(
                timestamp_raw.replace("Z", "+00:00")
            )
        except Exception:
            timestamp = datetime.now(timezone.utc)

        author = item.get("authorDetails", {})
        return YouTubeChatMessage(
            message_id=item.get("id", ""),
            author_channel_id=author.get("channelId", ""),
            author_display_name=author.get("displayName", "Viewer"),
            text=text,
            timestamp=timestamp,
        )


class _BoundedSet:
    """Insertion-ordered set that forgets its oldest entries past a cap.

    Seen and answered ids otherwise grow forever during long unattended runs.
    """

    def __init__(self, cap: int):
        self.cap = cap
        self._items: "OrderedDict[str, None]" = OrderedDict()

    def add(self, item: str) -> None:
        self._items[item] = None
        self._items.move_to_end(item)
        while len(self._items) > self.cap:
            self._items.popitem(last=False)

    def discard(self, item: str) -> None:
        self._items.pop(item, None)

    def __contains__(self, item: object) -> bool:
        return item in self._items

    def __len__(self) -> int:
        return len(self._items)


# Our own channel posts "your game is up" links into chat. Reading those back
# made Mika build games for herself. Those posts are recognised (and every
# text the system posted itself), so the channel owner can now chat with the
# girls like anyone else (VR_IGNORE_OWNER=1 ignores the owner again).
# VR_BANNED_AUTHORS (comma separated names) are never read at all.
_OUR_POSTS: "deque[str]" = deque(maxlen=50)


REPEAT_WINDOW = 30.0  # the same text more than REPEAT_LIMIT times within this many seconds is spam
REPEAT_LIMIT = 3


def note_our_post(text: str) -> None:
    _OUR_POSTS.append(_normalize_message(text))

OWN_ANNOUNCEMENT_RE = re.compile(
    r"your (game|update) is (up|live|pushed)|every game mika and luna built", re.IGNORECASE
)


def _author_key(name: str) -> str:
    return re.sub(r"\s+", "", str(name or "").strip().lstrip("@").lower())


def banned_authors() -> set[str]:
    raw = os.environ.get("VR_BANNED_AUTHORS", "")
    return {_author_key(n) for n in raw.split(",") if _author_key(n)}


def blocked_author_reason(message: "YouTubeChatMessage") -> str:
    if _author_key(message.author_display_name) in banned_authors():
        return "banned"
    ignore_owner = os.environ.get("VR_IGNORE_OWNER", "0").strip().lower() in ("1", "true", "yes", "on")
    if str(getattr(message, "author_type", "") or "") == "owner":
        if ignore_owner:
            return "channel owner"
        if _normalize_message(message.text or "") in _OUR_POSTS:
            return "our own post"
    if OWN_ANNOUNCEMENT_RE.search(message.text or "") and "github.io" in (
        message.text or ""
    ):
        return "our own announcement"
    return ""


BUILD_REQUEST_RE = re.compile(
    r"\b(make|build|create|code|program|write|design|add|change|fix|update)\b.{0,60}?"
    r"\b(game|site|website|page|app|chart|animation|program|script|portfolio|landing|it|this)\b",
    re.IGNORECASE,
)


class YouTubeMessageBuffer:
    UNSAFE_RE = re.compile(
        r"\b(kill yourself|suicide|nazi|terrorist|rape|porn|onlyfans|slur)\b",
        re.IGNORECASE,
    )

    def __init__(
        self,
        max_messages: int,
        message_buffer_seconds: int,
        same_user_cooldown_seconds: int,
    ):
        self.max_messages = max_messages
        self.message_buffer_seconds = message_buffer_seconds
        self.same_user_cooldown_seconds = same_user_cooldown_seconds
        self.messages: Deque[YouTubeChatMessage] = deque()
        self.seen_message_ids = _BoundedSet(20000)
        self.buffered_message_ids: set[str] = set()
        self.answered_message_ids = _BoundedSet(5000)
        self.last_answered_by_author: dict[str, float] = {}
        # The same text from anyone, in the last REPEAT_WINDOW seconds. It used
        # to be counted forever (answered messages never counted down): after
        # two people typed "race" or "draw 7", nobody could for the whole stream.
        self.recent_normalized: Counter[str] = Counter()
        self._recent_texts: deque[tuple[float, str]] = deque()
        self.startup_cutoff = datetime.now(timezone.utc).timestamp() - STARTUP_BACKLOG_GRACE_SECONDS

    def add(self, message: YouTubeChatMessage) -> tuple[bool, str]:
        self.expire_stale()
        if not message.message_id:
            return False, "missing_id"
        if message.timestamp.timestamp() < self.startup_cutoff:
            return False, "startup_backlog"
        if message.message_id in self.buffered_message_ids:
            return False, "already_in_buffer"
        if message.message_id in self.seen_message_ids:
            return False, "duplicate_id"
        if message.message_id in self.answered_message_ids:
            return False, "already_answered"
        text = message.text.strip()
        if not text:
            return False, "empty"
        if len(text) > 280:
            return False, "too_long"
        normalized = _normalize_message(text)
        # Class mode quizzes are answered with one letter, by many people.
        quiz_answer = bool(QUIZ_ANSWER_RE.fullmatch(text)) and class_mode_enabled()
        if len(normalized) <= 1 and not quiz_answer:
            return False, "noise"
        if self.UNSAFE_RE.search(normalized):
            return False, "unsafe"
        self._forget_old_texts()
        paid = getattr(message, "kind", "text") in ("paid", "member")  # "Welcome to X!" for each new member is not spam
        if self.recent_normalized[normalized] >= REPEAT_LIMIT and not quiz_answer and not paid:
            return False, "repeated_spam"

        self.messages.append(message)
        self.seen_message_ids.add(message.message_id)
        self.buffered_message_ids.add(message.message_id)
        self.recent_normalized[normalized] += 1
        self._recent_texts.append((time.time(), normalized))
        while len(self.messages) > self.max_messages:
            removed = self.messages.popleft()
            self.buffered_message_ids.discard(removed.message_id)
        return True, "accepted"

    def expire_stale(self) -> None:
        cutoff = datetime.now(timezone.utc).timestamp() - self.message_buffer_seconds
        while self.messages and self.messages[0].timestamp.timestamp() < cutoff:
            stale = self.messages.popleft()
            self.buffered_message_ids.discard(stale.message_id)
        self._forget_old_texts()

    def _forget_old_texts(self) -> None:
        cutoff = time.time() - REPEAT_WINDOW
        while self._recent_texts and self._recent_texts[0][0] < cutoff:
            _at, normalized = self._recent_texts.popleft()
            self.recent_normalized[normalized] -= 1
            if self.recent_normalized[normalized] <= 0:
                del self.recent_normalized[normalized]

    def mark_answered(self, message: YouTubeChatMessage) -> None:
        self.answered_message_ids.add(message.message_id)
        if message.author_channel_id:
            self.last_answered_by_author[message.author_channel_id] = time.time()
            if len(self.last_answered_by_author) > 2000:
                cutoff = time.time() - self.same_user_cooldown_seconds
                self.last_answered_by_author = {
                    k: v for k, v in self.last_answered_by_author.items() if v >= cutoff
                }
        self.messages = deque(
            msg for msg in self.messages if msg.message_id != message.message_id
        )
        self.buffered_message_ids.discard(message.message_id)

    def get_eligible(self, limit: int) -> list[YouTubeChatMessage]:
        self.expire_stale()
        eligible = [
            message
            for message in self.messages
            if message.message_id not in self.answered_message_ids
        ]
        eligible.sort(
            key=lambda message: (
                _message_priority_score(message),
                min(message.age_seconds / 90, 1.0),
            ),
            reverse=True,
        )
        return eligible[:limit]

    def author_recently_answered(self, author_channel_id: str) -> bool:
        if not author_channel_id:
            return False
        last_answered = self.last_answered_by_author.get(author_channel_id)
        if not last_answered:
            return False
        return (time.time() - last_answered) < self.same_user_cooldown_seconds


class YouTubeMessageSelector:
    async def select(
        self,
        messages: list[YouTubeChatMessage],
        recently_answered: Callable[[str], bool],
    ) -> SelectionResult:
        if not messages:
            return SelectionResult(None, "no messages", 0.0)

        best_message = None
        best_score = -999.0
        for message in messages:
            score = _message_priority_score(message)
            if recently_answered(message.author_channel_id):
                score -= 1.0
            score -= min(message.age_seconds / 180, 1.0)
            if score > best_score:
                best_score = score
                best_message = message
        if not best_message:
            return SelectionResult(None, "no messages", 0.0)
        return SelectionResult(
            best_message.message_id,
            f"highest priority score {best_score:.2f}",
            max(0.0, min(1.0, (best_score + 3.0) / 8.0)),
        )


class YouTubeLiveChatService:
    """Ingests YouTube chat, picks worthwhile messages and hands them to the character.

    Chat can come from two sources:

    * ``playwright`` (default): reads the public live chat page in headless
      Chromium, event-driven, no API quota.
    * ``api``: the YouTube Data API poller kept for compatibility.
    """

    def __init__(self, config, default_context, connection_provider):
        self.config = config
        self.default_context = default_context
        self.connection_provider = connection_provider
        self.chat_source = (getattr(config, "chat_source", "playwright") or "playwright").lower()
        self.client = YouTubeLiveChatClient(
            api_key=config.api_key,
            channel_id=config.channel_id,
            video_id=config.video_id,
            prefer_stream_list=config.prefer_stream_list,
        )
        self.playwright_source = None
        if self.chat_source == "playwright":
            from .youtube_playwright_chat import YouTubePlaywrightChatSource

            self.playwright_source = YouTubePlaywrightChatSource(config)
        self.buffer = YouTubeMessageBuffer(
            max_messages=config.max_buffer_messages,
            message_buffer_seconds=config.message_buffer_seconds,
            same_user_cooldown_seconds=config.same_user_cooldown_seconds,
        )
        self.selector = YouTubeMessageSelector()
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._live_chat_id: Optional[str] = None
        self._last_response_completed_at = 0.0
        self._last_message_seen_at = time.time()
        self._last_response_debug_at = 0.0
        self._message_event = asyncio.Event()
        self._received_at: dict[str, float] = {}

    def enabled(self) -> bool:
        return bool(self.config.youtube_live_enabled)

    def _source_ready(self) -> tuple[bool, str]:
        if self.playwright_source:
            return self.playwright_source.ready()
        if self.client.ready():
            return True, ""
        return False, "api_key and channel_id or video_id are required for chat_source=api"

    async def start(self) -> None:
        if not self.enabled():
            logger.info("YouTube Live mode disabled.")
            return
        ready, reason = self._source_ready()
        if not ready:
            logger.warning(f"YouTube Live mode enabled but not configured: {reason}.")
            runtime.set(VRAgentState.ERROR_RECOVERABLE, f"not configured: {reason}")
            return
        if self._running:
            return
        self._running = True
        register = getattr(self.connection_provider, "register_chat_service", None)
        if register:
            register(self)
        runtime.set(VRAgentState.STARTING, f"chat source {self.chat_source}")
        logger.info(f"YouTube Live mode started (chat_source={self.chat_source}).")
        ingest = (
            self._playwright_ingest_loop() if self.playwright_source else self._ingest_loop()
        )
        self._tasks = [
            asyncio.create_task(ingest, name="youtube-live-ingest"),
            asyncio.create_task(self._response_loop(), name="youtube-live-response"),
        ]

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self.playwright_source:
            await self.playwright_source.stop()

    def _observe(self, message: YouTubeChatMessage) -> bool:
        """Let the room consume game commands and answers before selection."""
        observer = getattr(self.connection_provider, "observe_live_message", None)
        if not observer:
            return False
        try:
            consumed = bool(observer(message))
        except Exception as exc:
            logger.error(f"Live message observer failed: {exc}")
            return False
        if consumed:
            self.buffer.mark_answered(message)
            logger.info(
                f"YouTube message from {message.author_display_name} handled by the room: "
                f"{_truncate(message.text, 60)}"
            )
        return consumed

    def _maybe_promote_gallery(self, message: YouTubeChatMessage) -> None:
        """Share the gallery link now and then when enough people are chatting."""
        try:
            from ..publishing.promo import promoter

            if promoter.note(message.author_display_name):
                asyncio.get_running_loop().run_in_executor(None, promoter.post)
        except Exception as exc:  # never let promotion disturb chat reading
            logger.debug(f"Gallery promo skipped: {exc}")

    def _accept(self, message: YouTubeChatMessage, source_label: str) -> bool:
        blocked = blocked_author_reason(message)
        if blocked:
            chat_feed.record(message.author_display_name, message.text, f"ignored: {blocked}")
            logger.debug(
                f"YouTube {source_label} ignored ({blocked}) from {message.author_display_name}"
            )
            return False
        accepted, reason = self.buffer.add(message)
        chat_feed.record(
            message.author_display_name, message.text, "received" if accepted else f"filtered: {reason}"
        )
        if accepted and self._observe(message):
            self._last_message_seen_at = time.time()
            return True
        if accepted:
            now = time.time()
            self._last_message_seen_at = now
            self._received_at[message.message_id] = now
            if len(self._received_at) > 500:
                for key in list(self._received_at)[:250]:
                    self._received_at.pop(key, None)
            self._message_event.set()
            logger.info(
                f"YouTube {source_label} from {message.author_display_name}: {_truncate(message.text)}"
            )
            self._maybe_promote_gallery(message)
        else:
            logger.debug(f"YouTube {source_label} filtered ({reason}): {_truncate(message.text, 60)}")
        return accepted

    async def inject_mock_message(
        self, author: str, message: str, author_channel_id: str = "mock-author"
    ) -> dict[str, Any]:
        mock = YouTubeChatMessage(
            message_id=f"mock-{int(time.time() * 1000)}",
            author_channel_id=author_channel_id,
            author_display_name=author or "Mock Viewer",
            text=message,
            timestamp=datetime.now(timezone.utc),
        )
        accepted, reason = self.buffer.add(mock)
        chat_feed.record(mock.author_display_name, mock.text, "received (test)" if accepted else f"filtered: {reason}")
        if accepted and self._observe(mock):
            self._last_message_seen_at = time.time()
            return {"accepted": True, "reason": "handled_by_room", "message_id": mock.message_id}
        if accepted:
            self._last_message_seen_at = time.time()
            self._received_at[mock.message_id] = time.time()
            self._message_event.set()
            logger.info(
                f"YouTube mock message accepted from {mock.author_display_name}: {_truncate(mock.text)}"
            )
        else:
            logger.info(f"YouTube mock message filtered: {reason}")
        return {"accepted": accepted, "reason": reason, "message_id": mock.message_id}

    def status(self) -> dict[str, Any]:
        status = {
            "enabled": self.enabled(),
            "running": self._running,
            "chat_source": self.chat_source,
            "live_chat_connected": bool(self._live_chat_id)
            if not self.playwright_source
            else self.playwright_source.health.chat_attached,
            "buffer_size": len(self.buffer.messages),
            "vr_agent": runtime.snapshot(),
            "latency": runtime.latency.summary(),
        }
        if self.playwright_source:
            status["playwright"] = self.playwright_source.status()
        return status

    def _debug_response_loop(self, message: str, force: bool = False) -> None:
        now = time.time()
        if force or now - self._last_response_debug_at >= 5.0:
            logger.debug(message)
            self._last_response_debug_at = now

    async def _playwright_ingest_loop(self) -> None:
        async def on_messages(messages: list[YouTubeChatMessage]) -> None:
            for message in messages:
                self._accept(message, "message")

        await self.playwright_source.run(on_messages)

    async def _ingest_loop(self) -> None:
        while self._running:
            try:
                if not self._live_chat_id:
                    logger.info("Waiting for active YouTube livestream...")
                    runtime.set(VRAgentState.WAITING_FOR_STREAM, "api discovery")
                    self._live_chat_id = await self.client.discover_active_live_chat_id()
                    if not self._live_chat_id:
                        # API search costs 100 quota units per call; never poll it fast.
                        await asyncio.sleep(max(30, self.config.discovery_retry_seconds))
                        continue
                    logger.info("YouTube liveChatId acquired; chat connection established.")
                    runtime.set(VRAgentState.IDLE, "api chat connected")

                async for messages in self.client.iter_messages(self._live_chat_id):
                    if not self._running:
                        break
                    for message in messages:
                        self._accept(message, "message")
            except YouTubeLiveChatEnded as exc:
                logger.warning(f"YouTube chat disconnected: {exc}. Reconnecting...")
                runtime.set(VRAgentState.WAITING_FOR_STREAM, "chat ended")
                self._live_chat_id = None
                self.client.next_page_token = None
                await asyncio.sleep(max(30, self.config.discovery_retry_seconds))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"YouTube API/chat error: {exc}. Reconnecting...")
                runtime.set(VRAgentState.RECONNECTING, str(exc)[:200])
                await asyncio.sleep(min(self.config.discovery_retry_seconds, 30))

    async def _wait_for_work(self, timeout: float) -> None:
        """Wake immediately on new chat instead of a fixed polling tick."""
        try:
            await asyncio.wait_for(self._message_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        self._message_event.clear()

    async def _response_loop(self) -> None:
        while self._running:
            try:
                await self._wait_for_work(1.0)
                if runtime.paused:
                    continue  # be-right-back mode: chat is kept, no replies
                buffer_size = len(self.buffer.messages)
                if not self.connection_provider.has_connected_clients():
                    self._debug_response_loop(
                        f"YouTube response loop waiting: no connected frontend client; buffer_size={buffer_size}",
                        force=buffer_size > 0,
                    )
                    continue
                if self._route_to_class():
                    continue
                if not self.connection_provider.is_idle():
                    self._maybe_side_chat()
                    continue
                cooldown_remaining = self.config.response_cooldown_seconds - (
                    time.time() - self._last_response_completed_at
                )
                if cooldown_remaining > 0:
                    if buffer_size:
                        await asyncio.sleep(min(cooldown_remaining, 1.0))
                        self._message_event.set()
                    continue

                messages = self.buffer.get_eligible(self.config.selector_max_messages)
                if not messages:
                    await self._maybe_idle_banter()
                    continue

                selection = await self.selector.select(
                    messages, self.buffer.author_recently_answered
                )
                if not selection.selected_message_id:
                    continue
                selected = next(
                    (m for m in messages if m.message_id == selection.selected_message_id),
                    None,
                )
                if not selected:
                    continue

                logger.info(
                    f"YouTube message selected: id={selected.message_id}, "
                    f"confidence={selection.confidence:.2f}, reason={selection.reason}"
                )
                received_at = self._received_at.pop(selected.message_id, time.time())
                # Mark before responding so a slow response cannot answer twice.
                self.buffer.mark_answered(selected)
                completed = await self.connection_provider.process_youtube_live_message(
                    selected,
                    max_wait_seconds=MAX_RESPONSE_WAIT_SECONDS,
                    received_at=received_at,
                )
                self._last_response_completed_at = time.time()
                if completed:
                    logger.info("YouTube response accepted by conversation pipeline.")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"YouTube response loop error: {exc}")
                runtime.set(VRAgentState.ERROR_RECOVERABLE, f"response loop: {exc}"[:200])
                await asyncio.sleep(1.0)

    def _route_to_class(self) -> bool:
        """Class mode: every waiting message goes to the class (answered
        between lesson steps, or counted as a quiz answer)."""
        active = getattr(self.connection_provider, "class_active", None)
        if not active or not active():
            return False
        for message in self.buffer.get_eligible(self.config.selector_max_messages):
            self.buffer.mark_answered(message)
            logger.info(f"Class chat from {message.author_display_name}: {_truncate(message.text, 60)}")
            received_at = self._received_at.pop(message.message_id, time.time())
            try:
                self.connection_provider.class_message(message, received_at=received_at)
            except TypeError:
                self.connection_provider.class_message(message)
        return True

    def _maybe_side_chat(self) -> None:
        """While Mika or Luna is coding, the other one still answers chat.

        Each message gets at most one side reply. Plain chat is answered and
        done; a new build request is told it is next and stays queued, so it
        is built when the current build finishes.
        """
        ready = getattr(self.connection_provider, "side_chat_ready", None)
        if not ready or not ready():
            return
        done = getattr(self, "_side_answered", None)
        if done is None:
            done = self._side_answered = _BoundedSet(500)
        waiting = [
            m
            for m in self.buffer.get_eligible(self.config.selector_max_messages)
            if m.message_id not in done
        ]
        if not waiting:
            return
        message = waiting[-1]  # the newest: it is what chat is looking at
        done.add(message.message_id)
        wants_build = bool(BUILD_REQUEST_RE.search(message.text or ""))
        if not wants_build:
            self.buffer.mark_answered(message)
        logger.info(
            f"YouTube side reply during a build to {message.author_display_name}"
            f"{' (queued as next build)' if wants_build else ''}: {_truncate(message.text, 60)}"
        )
        self.connection_provider.side_chat(message, wants_build)

    async def _maybe_idle_banter(self) -> None:
        # Zero-activity rule: with no viewer messages the stream makes no LLM
        # or TTS requests at all, so the old quiet-chat banter never runs.
        if self.config.idle_banter_enabled and not getattr(self, "_banter_warned", False):
            self._banter_warned = True
            logger.warning(
                "idle_banter_enabled is ignored: VR Agent never calls the LLM or TTS "
                "without viewer activity."
            )
        return
