"""YouTube live chat reader driven by Playwright.

Reads the public live chat page (``/live_chat?is_popout=1&v=VIDEO_ID``) in a
headless Chromium instead of polling the YouTube Data API, so there is no
quota to run out of during long unattended streams.

How it works:

1. Find the active livestream: a configured ``video_id``, otherwise the
   channel's ``/live`` page (``channel_id`` or ``channel_handle``).
2. Open the popout chat, inject ``youtube_chat_observer.js``, which attaches a
   MutationObserver to the chat item list and pushes new messages back through
   an exposed binding. Delivery is event-driven; nothing is re-scraped.
3. Switch the chat from "Top chat" to "Live chat" once after navigation when
   that works; carry on with whatever mode is active if it does not.
4. A watchdog heartbeats the page. A closed or crashed page, a stuck
   evaluation, a chat that ended, or a new stream on the channel all lead to a
   clean restart with exponential backoff. The browser page is also recycled
   every few hours to keep Chromium memory flat over long runs.
"""

from __future__ import annotations

import asyncio
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from ..vr_agent.state import VRAgentState, runtime
from .youtube_live import YouTubeChatMessage

OBSERVER_JS = (Path(__file__).with_name("youtube_chat_observer.js")).read_text(
    encoding="utf-8"
)
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_HANDLE_RE = re.compile(r"^@?[A-Za-z0-9._-]{3,100}$")

MessagesCallback = Callable[[list[YouTubeChatMessage]], Awaitable[None]]

LIVE_CHAT_URL = "https://www.youtube.com/live_chat?is_popout=1&v={video_id}"
CHANNEL_LIVE_URLS = {
    "channel": "https://www.youtube.com/channel/{value}/live",
    "handle": "https://www.youtube.com/@{value}/live",
}

_DISCOVERY_JS = """
() => {
  const r = window.ytInitialPlayerResponse;
  const vd = r && r.videoDetails;
  const canonical = (document.querySelector('link[rel="canonical"]') || {}).href || '';
  const m = canonical.match(/[?&]v=([A-Za-z0-9_-]{11})/);
  return {
    videoId: (vd && vd.videoId) || (m && m[1]) || null,
    isLive: !!(vd && vd.isLive),
    isUpcoming: !!(vd && vd.isUpcoming),
    hasDetails: !!vd,
    title: (vd && vd.title) || '',
    url: location.href,
  };
}
"""

_MODE_TRIGGER_SELECTORS = [
    "#live-chat-view-selector-sub-menu #label",
    "yt-live-chat-header-renderer yt-sort-filter-sub-menu-renderer #label",
    "yt-live-chat-header-renderer tp-yt-paper-button#label",
]
_MODE_LABEL_SELECTORS = [
    "#live-chat-view-selector-sub-menu #label-text",
    "yt-live-chat-header-renderer #label-text",
]
_MODE_ITEM_SELECTOR = (
    "tp-yt-iron-dropdown tp-yt-paper-listbox a, yt-dropdown-menu tp-yt-paper-listbox a"
)


def desktop_user_agent(version: str) -> str:
    """Headless Chromium announces itself as "HeadlessChrome", and YouTube's
    live chat answers that with an "update your browser" page instead of the
    chat. A regular desktop Chrome user agent for the same version fixes it."""
    major = (version or "130").split(".")[0]
    if sys.platform == "darwin":
        platform = "Macintosh; Intel Mac OS X 10_15_7"
    elif sys.platform.startswith("win"):
        platform = "Windows NT 10.0; Win64; x64"
    else:
        platform = "X11; Linux x86_64"
    return (
        f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{major}.0.0.0 Safari/537.36"
    )


@dataclass
class PlaywrightChatHealth:
    """Developer monitoring only. Never broadcast to the stream overlay."""

    browser_running: bool = False
    chat_attached: bool = False
    video_id: Optional[str] = None
    chat_mode: str = "unknown"
    selector: Optional[str] = None
    messages_received: int = 0
    last_message_at: float = 0.0
    last_heartbeat_at: float = 0.0
    last_heartbeat_ms: float = 0.0
    attached_at: float = 0.0
    restarts: int = 0
    consecutive_failures: int = 0
    last_error: str = ""
    history: list[str] = field(default_factory=list)

    def note(self, event: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.history.append(f"{stamp} {event}")
        del self.history[:-20]

    def to_dict(self) -> dict[str, Any]:
        now = time.time()
        return {
            "browser_running": self.browser_running,
            "chat_attached": self.chat_attached,
            "video_id": self.video_id,
            "chat_mode": self.chat_mode,
            "selector": self.selector,
            "messages_received": self.messages_received,
            "seconds_since_last_message": round(now - self.last_message_at, 1)
            if self.last_message_at
            else None,
            "seconds_since_heartbeat": round(now - self.last_heartbeat_at, 1)
            if self.last_heartbeat_at
            else None,
            "heartbeat_ms": round(self.last_heartbeat_ms, 1),
            "attached_for_seconds": round(now - self.attached_at, 1)
            if self.attached_at
            else None,
            "restarts": self.restarts,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "recent_events": self.history[-10:],
        }


class ChatEnded(Exception):
    """The chat page reports the stream or chat is over."""


class StreamChanged(Exception):
    """The channel is now live on a different video."""


class YouTubePlaywrightChatSource:
    """Long-running Playwright chat reader with self-recovery."""

    def __init__(self, config) -> None:
        self.config = config
        self.health = PlaywrightChatHealth()
        self._on_messages: Optional[MessagesCallback] = None
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._running = False
        self._stable_since = 0.0
        self._last_waiting_log = 0.0
        self._last_attached_video: Optional[str] = None

    # ------------------------------------------------------------------ api
    def ready(self) -> tuple[bool, str]:
        if self.config.video_id and not _VIDEO_ID_RE.match(str(self.config.video_id)):
            return False, "video_id is not an 11-character YouTube video id"
        if self.config.video_id:
            return True, ""
        channel = self._channel_target()
        if channel:
            return True, ""
        return False, "set video_id, channel_id (UC...) or channel_handle (@name)"

    def status(self) -> dict[str, Any]:
        return self.health.to_dict()

    async def run(self, on_messages: MessagesCallback) -> None:
        """Supervisor loop. Returns only when cancelled."""
        self._on_messages = on_messages
        self._running = True
        backoff = self.config.playwright_restart_min_seconds
        try:
            while self._running:
                try:
                    await self._ensure_browser()
                    video_id = await self._discover_video_id()
                    if not video_id:
                        runtime.set(
                            VRAgentState.WAITING_FOR_STREAM,
                            "no active livestream found",
                        )
                        if time.time() - self._last_waiting_log > 300:
                            self._last_waiting_log = time.time()
                            logger.info(
                                "Waiting for your YouTube livestream to start. "
                                f"Checking every {self.config.discovery_retry_seconds}s."
                            )
                        await asyncio.sleep(self.config.discovery_retry_seconds)
                        continue
                    await self._attach_chat(video_id)
                    self.health.consecutive_failures = 0
                    backoff = self.config.playwright_restart_min_seconds
                    await self._watch(video_id)
                except asyncio.CancelledError:
                    raise
                except ChatEnded as exc:
                    self.health.note(f"chat ended: {exc}")
                    logger.warning(
                        f"YouTube chat ended or unavailable: {exc}. Waiting for stream."
                    )
                    runtime.set(VRAgentState.WAITING_FOR_STREAM, "chat ended")
                    await self._close_page()
                    await asyncio.sleep(self.config.discovery_retry_seconds)
                except StreamChanged as exc:
                    self.health.note(f"stream changed: {exc}")
                    logger.info(
                        f"Channel went live on a new video ({exc}); switching chat."
                    )
                    await self._close_page()
                except Exception as exc:
                    self.health.restarts += 1
                    self.health.consecutive_failures += 1
                    self.health.last_error = f"{type(exc).__name__}: {exc}"[:300]
                    self.health.note(f"error: {self.health.last_error}")
                    logger.error(
                        f"YouTube Playwright chat failure #{self.health.consecutive_failures}: "
                        f"{self.health.last_error}. Restarting in {backoff:.0f}s."
                    )
                    runtime.set(VRAgentState.RECONNECTING, self.health.last_error)
                    hard_reset = self.health.consecutive_failures >= 3
                    await self._teardown(full=hard_reset)
                    await asyncio.sleep(backoff + random.uniform(0, backoff * 0.25))
                    backoff = min(
                        backoff * 2, self.config.playwright_restart_max_seconds
                    )
        finally:
            self._running = False
            await self._teardown(full=True)

    async def stop(self) -> None:
        self._running = False
        await self._teardown(full=True)

    # ------------------------------------------------------------ browser
    async def _ensure_browser(self) -> None:
        if self._context and self._browser_alive():
            return
        await self._teardown(full=True)
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - depends on install
            raise RuntimeError(
                "Playwright is not installed. Run: uv add playwright && uv run playwright install chromium"
            ) from exc

        self._playwright = await async_playwright().start()
        launch_args = [
            "--mute-audio",
            "--disable-dev-shm-usage",
            "--autoplay-policy=no-user-gesture-required",
            "--disable-blink-features=AutomationControlled",
        ]
        common = dict(
            headless=self.config.playwright_headless,
            args=launch_args,
        )
        context_opts = dict(
            locale="en-US",
            viewport={"width": 420, "height": 720},
        )
        if self.config.playwright_user_data_dir:
            user_dir = Path(self.config.playwright_user_data_dir).expanduser()
            user_dir.mkdir(parents=True, exist_ok=True)
            context_opts["user_agent"] = await self._user_agent(common)
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(user_dir), **common, **context_opts
            )
            self._browser = None
        else:
            self._browser = await self._playwright.chromium.launch(**common)
            context_opts["user_agent"] = self.config.playwright_user_agent or (
                desktop_user_agent(self._browser.version)
            )
            self._context = await self._browser.new_context(**context_opts)
        # Block heavy resources; the chat DOM is all we need.
        await self._context.route("**/*", self._route_filter)
        await self._context.expose_binding("__vrAgentEmit", self._on_binding)
        self.health.browser_running = True
        self.health.note("browser started")
        logger.info("YouTube Playwright browser started.")

    async def _user_agent(self, launch_opts: dict) -> str:
        """Configured UA, or a normal desktop Chrome UA for this Chromium build."""
        if self.config.playwright_user_agent:
            return self.config.playwright_user_agent
        probe = await self._playwright.chromium.launch(**launch_opts)
        try:
            return desktop_user_agent(probe.version)
        finally:
            await probe.close()

    def _browser_alive(self) -> bool:
        if self._browser is not None:
            return self._browser.is_connected()
        return self._context is not None

    @staticmethod
    async def _route_filter(route) -> None:
        # The chat DOM is all we need. Emoji alt text is already in the DOM,
        # so images, media and fonts are skipped to keep the page light.
        if route.request.resource_type in {"image", "media", "font"}:
            await route.abort()
            return
        await route.continue_()

    async def _new_page(self):
        page = await self._context.new_page()
        page.set_default_timeout(20000)
        page.on("crash", lambda *_: self.health.note("page crashed"))
        return page

    async def _close_page(self) -> None:
        page, self._page = self._page, None
        self.health.chat_attached = False
        if page:
            try:
                await page.close()
            except Exception:
                pass

    async def _teardown(self, full: bool) -> None:
        await self._close_page()
        if not full:
            return
        for closer in (self._context, self._browser):
            if closer is None:
                continue
            try:
                await closer.close()
            except Exception:
                pass
        self._context = None
        self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._playwright = None
        self.health.browser_running = False

    # ---------------------------------------------------------- discovery
    def _channel_target(self) -> Optional[tuple[str, str]]:
        channel_id = str(self.config.channel_id or "").strip()
        handle = str(self.config.channel_handle or "").strip()
        if (
            channel_id
            and not channel_id.startswith("${")
            and _CHANNEL_ID_RE.match(channel_id)
        ):
            return "channel", channel_id
        if handle and _HANDLE_RE.match(handle):
            return "handle", handle.lstrip("@")
        return None

    async def _discover_video_id(self) -> Optional[str]:
        if self.config.video_id:
            return str(self.config.video_id)
        target = self._channel_target()
        if not target:
            return None
        kind, value = target
        info = await self._probe_channel_live(kind, value)
        if not info:
            return None
        if info.get("isLive") or (not info.get("hasDetails") and info.get("videoId")):
            logger.info(
                f"YouTube livestream detected: video_id={info['videoId']} {info.get('title', '')!r}"
            )
            return info["videoId"]
        if info.get("isUpcoming"):
            logger.info(f"Stream {info.get('videoId')} is scheduled but not live yet.")
        return None

    async def _probe_channel_live(self, kind: str, value: str) -> Optional[dict]:
        url = CHANNEL_LIVE_URLS[kind].format(value=value)
        page = await self._new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            if "consent." in page.url:
                raise RuntimeError(
                    "YouTube consent page shown; set playwright_user_data_dir and accept once with headless off"
                )
            info = await page.evaluate(_DISCOVERY_JS)
            return info if info and info.get("videoId") else None
        finally:
            await page.close()

    # --------------------------------------------------------------- chat
    async def _attach_chat(self, video_id: str) -> None:
        runtime.set(
            VRAgentState.RECONNECTING
            if self.health.restarts
            else VRAgentState.STARTING,
            "opening chat",
        )
        await self._close_page()
        page = await self._new_page()
        self._page = page
        await page.goto(
            LIVE_CHAT_URL.format(video_id=video_id), wait_until="domcontentloaded"
        )
        try:
            await page.wait_for_selector(
                "yt-live-chat-item-list-renderer #items, #item-list #items",
                state="attached",
                timeout=self.config.playwright_chat_load_timeout_seconds * 1000,
            )
        except Exception:
            hint = await self._safe_eval_text(page)
            raise ChatEnded(
                hint or "live chat did not load (stream offline or chat disabled)"
            )

        if self.config.prefer_live_chat_mode:
            await self._select_live_chat_mode(page)

        # New stream: answer the newest few messages already in chat. Same
        # stream after a reconnect: skip them, they were already seen.
        backlog = (
            int(self.config.answer_backlog_on_start)
            if video_id != self._last_attached_video
            else 0
        )
        await page.evaluate("n => { window.__vrAgentBacklog = n; }", backlog)
        heartbeat = await page.evaluate(OBSERVER_JS)
        if not heartbeat or not heartbeat.get("attached"):
            raise RuntimeError("chat observer could not attach to the message list")
        self.health.video_id = video_id
        self._last_attached_video = video_id
        self.health.chat_attached = True
        self.health.selector = heartbeat.get("selector")
        self.health.attached_at = time.time()
        self.health.last_heartbeat_at = time.time()
        self._stable_since = time.time()
        self.health.note(f"attached to {video_id} via {self.health.selector}")
        logger.info(
            f"YouTube live chat attached (video_id={video_id}, mode={self.health.chat_mode}, "
            f"selector={self.health.selector}, recent answered={backlog}, older skipped={max(0, heartbeat.get('seen', 0))})."
        )
        runtime.set(VRAgentState.IDLE, "chat attached")

    async def _select_live_chat_mode(self, page) -> None:
        """Switch Top chat -> Live chat once. Failure is logged, never fatal."""
        try:
            label_before = await self._read_mode_label(page)
            if label_before and re.match(r"\s*live chat", label_before, re.I):
                self.health.chat_mode = "live"
                return
            trigger = None
            for selector in _MODE_TRIGGER_SELECTORS:
                candidate = page.locator(selector).first
                if await candidate.count():
                    trigger = candidate
                    break
            if trigger is None:
                self.health.chat_mode = "top (selector not found)"
                logger.info(
                    "Live chat mode selector not found; keeping current chat mode."
                )
                return
            await trigger.click(timeout=5000)
            items = page.locator(_MODE_ITEM_SELECTOR)
            await items.first.wait_for(state="visible", timeout=5000)
            count = await items.count()
            chosen = None
            for i in range(count):
                text = (await items.nth(i).inner_text()).strip()
                if re.match(r"live chat", text, re.I):
                    chosen = items.nth(i)
                    break
            if chosen is None and count >= 2:
                chosen = items.nth(1)  # YouTube lists Top chat first, Live chat second
            if chosen is None:
                await page.keyboard.press("Escape")
                self.health.chat_mode = "top (no live option)"
                return
            await chosen.click(timeout=5000)
            await page.wait_for_timeout(800)
            label_after = await self._read_mode_label(page)
            if label_after and re.match(r"\s*live chat", label_after, re.I):
                self.health.chat_mode = "live"
                logger.info("YouTube chat switched to Live chat.")
            else:
                self.health.chat_mode = f"unconfirmed ({label_after or 'no label'})"
                logger.info(
                    f"Live chat selection not confirmed (label={label_after!r}); continuing."
                )
        except Exception as exc:
            self.health.chat_mode = "top (switch failed)"
            logger.info(
                f"Could not switch to Live chat ({exc}); continuing with current mode."
            )
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass

    @staticmethod
    async def _read_mode_label(page) -> str:
        for selector in _MODE_LABEL_SELECTORS:
            loc = page.locator(selector).first
            try:
                if await loc.count():
                    return (await loc.inner_text()).strip()
            except Exception:
                continue
        return ""

    @staticmethod
    async def _safe_eval_text(page) -> str:
        try:
            text = await page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 300)"
            )
            return re.sub(r"\s+", " ", text).strip()
        except Exception:
            return ""

    async def _watch(self, video_id: str) -> None:
        page = self._page
        interval = self.config.playwright_heartbeat_seconds
        last_live_check = time.time()
        recycle_after = self.config.playwright_page_recycle_hours * 3600
        while self._running:
            await asyncio.sleep(interval)
            if page is None or page.is_closed():
                raise RuntimeError("chat page closed")
            started = time.perf_counter()
            try:
                beat = await asyncio.wait_for(
                    page.evaluate(OBSERVER_JS), timeout=interval * 2 + 5
                )
            except asyncio.TimeoutError:
                raise RuntimeError("chat page heartbeat timed out")
            self.health.last_heartbeat_ms = (time.perf_counter() - started) * 1000
            self.health.last_heartbeat_at = time.time()
            if beat.get("ended_hint"):
                raise ChatEnded(beat["ended_hint"])
            if not beat.get("attached"):
                raise RuntimeError("chat list disappeared and could not be re-attached")
            if beat.get("reattached"):
                self.health.note("observer re-attached")
                logger.info("YouTube chat list was replaced; observer re-attached.")

            if time.time() - self._stable_since > 300:
                self.health.consecutive_failures = 0

            if recycle_after and time.time() - self.health.attached_at > recycle_after:
                self.health.note("scheduled page recycle")
                logger.info("Recycling YouTube chat page (scheduled).")
                await self._close_page()
                return

            check_every = self.config.live_check_interval_seconds
            if (
                check_every
                and not self.config.video_id
                and time.time() - last_live_check > check_every
                and time.time() - (self.health.last_message_at or 0) > check_every
            ):
                last_live_check = time.time()
                target = self._channel_target()
                if target:
                    info = await self._probe_channel_live(*target)
                    current = (
                        info.get("videoId") if info and info.get("isLive") else None
                    )
                    if current and current != video_id:
                        raise StreamChanged(current)

    # ----------------------------------------------------------- delivery
    async def _on_binding(self, _source, batch) -> None:
        if not isinstance(batch, list) or not self._on_messages:
            return
        now = datetime.now(timezone.utc)
        messages: list[YouTubeChatMessage] = []
        for raw in batch[:200]:
            msg = self._to_message(raw, now)
            if msg:
                messages.append(msg)
        if not messages:
            return
        self.health.messages_received += len(messages)
        self.health.last_message_at = time.time()
        try:
            await self._on_messages(messages)
        except Exception as exc:
            logger.error(f"YouTube chat message handler failed: {exc}")

    def _to_message(self, raw: Any, now: datetime) -> Optional[YouTubeChatMessage]:
        if not isinstance(raw, dict):
            return None
        message_id = str(raw.get("id") or "")[:200]
        text = str(raw.get("text") or "")[:500]
        author = str(raw.get("author") or "").strip()[:100] or "Viewer"
        author_type = str(raw.get("author_type") or "")[:40]
        kind = str(raw.get("kind") or "text")[:20]
        amount = str(raw.get("amount") or "")[:40]
        if not message_id or (not text and not amount):
            return None
        if self.config.ignore_owner_messages and author_type == "owner":
            return None
        return YouTubeChatMessage(
            message_id=message_id,
            author_channel_id=f"name:{author.lower()}",
            author_display_name=author,
            text=text or f"(sent {amount})",
            timestamp=now,
            kind=kind,
            amount=amount,
            author_type=author_type,
        )
