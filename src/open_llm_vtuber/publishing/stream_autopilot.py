"""The stream runs itself: title, description, channel page and the 12 hour limit.

    YOUTUBE_AUTO_TITLE=1                 each lesson renames the live stream and
                                         rewrites its description (the games
                                         section is kept)
    YOUTUBE_AUTO_CHANNEL_DESCRIPTION=1   the channel's About text is set once at start
    VR_MAX_LIVE_MINUTES=715              say goodbye and end the stream at 11h55m
                                         (YouTube keeps no archive past 12 hours); 0 = off
    OBS_WEBSOCKET_URL / OBS_WEBSOCKET_PASSWORD
                                         when set, OBS is told to stop streaming too

Everything here needs the YouTube sign-in (scripts/youtube_authorize.py) and
YOUTUBE_PUBLISHING_ENABLED=true. With PUBLISHING_DRY_RUN=true it only logs what
it would do. Nothing here ever raises into the stream.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from . import description as desc
from .youtube import YouTubeError

CHANNEL_DESCRIPTION = """I build autonomous AI systems, coding agents, and automation experiments in public.

Right now I'm developing Mika and Luna, AI characters that talk with viewers, teach, write and run code, and create games, websites and visualizations live.

The focus is teaching and building in public with a Jupyter-style workflow: visible code, immediate results.

• AI agents • Python • JavaScript • Automation • Interactive teaching • Games and visualizations

The system is experimental, so you'll see the good results and the failures.

🔴 Live: https://www.youtube.com/@SelwynBuilds-j1s/live
🎮 Viewer projects: https://randomsci.github.io/mika-generated-games/
💻 GitHub: https://github.com/RandomSci/VR-Agent
📚 Free AI automation course: https://whop.com/selwyn-builds/exp_rc0jGvlp9zbLvh/app/
📬 selwyn@selwynbuilds.com"""

STREAM_BODY = """I build autonomous AI systems, coding agents, and automation experiments in public.

Right now I'm developing Mika and Luna, AI characters that can talk with viewers, teach, write and run code, create games, websites, visualizations, and experiments live.

The current focus is teaching and building in public using a lightweight coding workflow inspired by Jupyter-style interaction: fast iteration, visible code, immediate results, and fewer unnecessary processes in the background.

This channel documents the real development process:
• AI agents
• Python
• JavaScript
• Jupyter-style coding
• Automation
• Interactive teaching
• Browser tools
• Games and visualizations
• Building in public

The system is still experimental, so you'll see both the good results and the failures while I improve it.

🔴 WATCH THE AI AGENTS LIVE
https://www.youtube.com/@SelwynBuilds-j1s/live

🎮 VIEWER-BUILT PROJECTS
https://randomsci.github.io/mika-generated-games/

💻 GITHUB
https://github.com/RandomSci/VR-Agent

📚 FREE AI AUTOMATION COURSE
https://whop.com/selwyn-builds/exp_rc0jGvlp9zbLvh/app/

📬 CONTACT
selwyn@selwynbuilds.com

If you're interested in AI agents, coding, automation, and watching autonomous systems learn to build and teach in real time, subscribe and follow the project."""


# One playlist per show; every live of that show is added to it by itself.
PLAYLISTS = {
    "minecraft": (
        "Mika & Luna play Minecraft 🔴 LIVE",
        "Every livestream where Mika and Luna, two AI characters, play survival Minecraft by themselves "
        "while chat tells them what to do.",
    ),
    "class": (
        "Learn to Code LIVE with AI Teachers",
        "Every live class where Natori and Hibiki, two AI teachers, teach Python step by step with a live "
        "notebook. Each stream continues where the last one stopped.",
    ),
}
PLAYLIST_FILE = Path("data/youtube_playlists.json")
THUMBNAILS = {
    "minecraft": Path("assets/thumbnails/minecraft.jpg"),
    "class": Path("assets/thumbnails/class.jpg"),
}


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def stream_title(course: str, number: int, total: int, lesson: str) -> str:
    title = f"Learn {course} LIVE with AI Teachers 🔴 Lesson {number}: {lesson}"
    if len(title) > 100:
        title = f"Learn {course} LIVE 🔴 Lesson {number}: {lesson}"
    return title[:100]


def stream_description(course: str, number: int, total: int, lesson: str, goals: str = "") -> str:
    head = [
        f"📚 TODAY'S CLASS: {course}, lesson {number} of {total}, {lesson}",
    ]
    if goals:
        head.append(f"What we cover: {goals}")
    head += [
        "Ask questions in chat, Mika and Luna answer between steps. Quizzes: type A, B or C!",
        "",
    ]
    return "\n".join(head) + STREAM_BODY


class StreamAutopilot:
    def __init__(self, publisher: Any) -> None:
        self.publisher = publisher
        self.settings = publisher.settings
        self.auto_title = _flag("YOUTUBE_AUTO_TITLE", "1")
        self.auto_channel = _flag("YOUTUBE_AUTO_CHANNEL_DESCRIPTION", "1")
        self.max_minutes = float(os.environ.get("VR_MAX_LIVE_MINUTES", "715") or 0)
        self.goodbye: Optional[Callable[[str], Awaitable[None]]] = None
        self.shutdown: Optional[Callable[[], None]] = None
        self.task: Optional[asyncio.Task] = None
        self._last_title = ""
        self._thumb_video = ""
        self._playlisted: dict[str, str] = {}
        self._class_extras: Optional[asyncio.Task] = None
        self._ended = False
        self.farewell = "That's the end of today's stream! Thanks for learning with us, see you next session 👋"
        self.goodbye_timeout = 15.0

    # ------------------------------------------------------------ helpers
    @property
    def ready(self) -> bool:
        s = self.settings
        return bool(s.youtube_enabled and (s.dry_run or s.youtube_ready))

    async def _client_call(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.to_thread(fn, *args)

    def _video_id(self, client: Any) -> str:
        return self.publisher._video_id(client)

    # ------------------------------------------------------------ start
    def start(self) -> None:
        if not self.ready or (self.task and not self.task.done()):
            return
        self.task = asyncio.create_task(self._run(), name="stream-autopilot")

    async def _run(self) -> None:
        if self.auto_channel:
            await self.set_channel_description()
        if self.max_minutes <= 0:
            return
        logger.info(f"Stream autopilot: the stream ends by itself after {self.max_minutes:.0f} minutes live")
        started_at = 0.0
        while not self._ended:
            await asyncio.sleep(60)
            try:
                if not started_at:
                    started_at = await self._started_at()
                if started_at and time.time() - started_at >= self.max_minutes * 60:
                    await self.end_stream("limit")
                    return
            except Exception as exc:
                logger.debug(f"Stream autopilot check failed: {exc}")

    async def _started_at(self) -> float:
        if self.settings.dry_run:
            return 0.0
        client = self.publisher._youtube_factory()
        video_id = await self._client_call(self._video_id, client)
        if not video_id:
            return 0.0
        iso = await self._client_call(client.live_started_at, video_id)
        if not iso:
            return 0.0
        started = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        logger.info(f"Stream autopilot: live since {iso}")
        return started

    # ------------------------------------------------------------ channel
    async def set_channel_description(self) -> None:
        if self.settings.dry_run:
            logger.info("Dry run: would set the channel description")
            return
        try:
            client = self.publisher._youtube_factory()
            channel = await self._client_call(client.channel_branding)
            current = ((channel.get("brandingSettings") or {}).get("channel") or {}).get("description", "")
            if current.strip() == CHANNEL_DESCRIPTION.strip():
                return
            await self._client_call(client.set_channel_description, channel, CHANNEL_DESCRIPTION)
            logger.info("Stream autopilot: channel description updated")
        except YouTubeError as exc:
            logger.warning(f"Channel description not updated: {exc}")
        except Exception as exc:
            logger.warning(f"Channel description not updated: {exc}")

    # ------------------------------------------------------------ per lesson
    async def lesson_started(self, course: str, number: int, total: int, lesson: str, goals: str = "") -> None:
        title = stream_title(course, number, total, lesson)
        await self._rename(title, stream_description(course, number, total, lesson, goals))
        if self._class_extras is None or self._class_extras.done():
            self._class_extras = asyncio.create_task(self._class_extras_soon())

    async def _class_extras_soon(self) -> None:
        """Thumbnail and playlist, again a bit later if YouTube had no live video yet."""
        for delay in (0, 60, 180):
            await asyncio.sleep(delay)
            await self._show_extras("class")

    async def mode_started(self, title: str, head: str, thumbnail: str = "", show: str = "minecraft") -> None:
        """Minecraft and other modes: their own title, their text above the
        channel text, their thumbnail and their playlist."""
        self.goodbye_timeout = 35.0  # the goodbye also stops the game processes
        await self._rename(title[:100], head + STREAM_BODY)
        if thumbnail:
            await self.set_thumbnail(thumbnail)
        await self._show_extras(show, thumbnail=bool(thumbnail))

    async def _show_extras(self, show: str, thumbnail: bool = False) -> None:
        if not thumbnail and THUMBNAILS.get(show, Path("")).is_file():
            await self.set_thumbnail(str(THUMBNAILS[show]))
        await self.add_to_playlist(show)

    async def add_to_playlist(self, show: str) -> None:
        """Once per live video: into the show's playlist (made the first time).
        YOUTUBE_AUTO_PLAYLIST=0 turns it off."""
        if show not in PLAYLISTS or not (self.ready and _flag("YOUTUBE_AUTO_PLAYLIST", "1")):
            return
        if self.settings.dry_run:
            logger.info(f"Dry run: would add the stream to the playlist '{PLAYLISTS[show][0]}'")
            return
        try:
            client = self.publisher._youtube_factory()
            video_id = await self._client_call(self._video_id, client)
            if not video_id or self._playlisted.get(show) == video_id:
                return
            playlist_id = await self._playlist_id(client, show)
            if not playlist_id:
                return
            if not await self._client_call(client.playlist_has, playlist_id, video_id):
                await self._client_call(client.add_to_playlist, playlist_id, video_id)
                logger.info(f"Stream autopilot: added to the playlist '{PLAYLISTS[show][0]}'")
            self._playlisted[show] = video_id
        except Exception as exc:
            logger.warning(f"Playlist not updated: {exc}")

    async def _playlist_id(self, client: Any, show: str) -> str:
        saved: dict[str, str] = {}
        try:
            saved = json.loads(PLAYLIST_FILE.read_text())
        except Exception:
            pass
        if saved.get(show):
            return saved[show]
        title, description = PLAYLISTS[show]
        playlist_id = await self._client_call(client.find_playlist, title)
        if not playlist_id:
            playlist_id = await self._client_call(client.create_playlist, title, description)
            logger.info(f"Stream autopilot: playlist created: {title}")
        if playlist_id:
            saved[show] = playlist_id
            try:
                PLAYLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
                PLAYLIST_FILE.write_text(json.dumps(saved, indent=2))
            except Exception:
                pass
        return playlist_id

    async def set_thumbnail(self, path: str) -> None:
        """Once per live video: upload the mode's thumbnail (YOUTUBE_AUTO_THUMBNAIL=0 turns it off)."""
        if not (self.ready and _flag("YOUTUBE_AUTO_THUMBNAIL", "1")):
            return
        if self.settings.dry_run:
            logger.info(f"Dry run: would set the thumbnail {path}")
            return
        try:
            client = self.publisher._youtube_factory()
            video_id = await self._client_call(self._video_id, client)
            if not video_id or video_id == self._thumb_video:
                return
            image = Path(path).read_bytes()
            mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
            await self._client_call(client.set_thumbnail, video_id, image, mime)
            self._thumb_video = video_id
            logger.info(f"Stream autopilot: thumbnail set ({Path(path).name})")
        except Exception as exc:
            logger.warning(f"Thumbnail not set: {exc}")

    async def _rename(self, title: str, body: str) -> None:
        if not (self.ready and self.auto_title):
            return
        if title == self._last_title:
            return
        self._last_title = title
        if self.settings.dry_run:
            logger.info(f"Dry run: would rename the stream to: {title}")
            return
        try:
            client = self.publisher._youtube_factory()
            video_id = await self._client_call(self._video_id, client)
            if not video_id:
                logger.info("Stream autopilot: no live stream found to rename yet")
                self._last_title = ""
                return
            published = self.publisher.store.published()
            if published:
                body = desc.fit_description(
                    body, published, self.publisher._base_url() + "/", self.settings.description_latest
                )
            snippet = await self._client_call(client.get_snippet, video_id)
            await self._client_call(client.set_title_and_description, video_id, snippet, title, body)
            logger.info(f"Stream autopilot: stream renamed to: {title}")
        except Exception as exc:
            self._last_title = ""
            logger.warning(f"Stream title not updated: {exc}")

    # ------------------------------------------------------------ ending
    async def end_stream(self, reason: str = "limit") -> None:
        """Goodbye (chat message at the same time), the broadcast ends through
        the YouTube API, OBS stops and closes, then the server stops."""
        if self._ended:
            return
        self._ended = True
        logger.warning(f"Stream autopilot: ending the stream ({reason})")
        chat = asyncio.create_task(
            self.post_chat(self.farewell)
        )
        if self.goodbye:
            try:
                await asyncio.wait_for(self.goodbye(reason), timeout=self.goodbye_timeout)
            except Exception as exc:
                logger.warning(f"Goodbye failed: {exc}")
        try:
            await asyncio.wait_for(chat, timeout=5)
        except Exception:
            pass
        await asyncio.sleep(2)  # the last words reach viewers (stream delay)
        if not self.settings.dry_run and self.settings.youtube_ready:
            try:
                client = self.publisher._youtube_factory()
                video_id = await self._client_call(self._video_id, client)
                if video_id:
                    await self._client_call(client.end_broadcast, video_id)
                    logger.info("Stream autopilot: YouTube broadcast ended")
            except Exception as exc:
                logger.warning(f"Could not end the broadcast through YouTube: {exc}")
        from .obs_control import stop_and_close

        await stop_and_close()
        if self.shutdown:
            self.shutdown()

    async def post_chat(self, text: str) -> None:
        s = self.settings
        if not (s.youtube_enabled and s.youtube_chat_enabled):
            return
        if s.dry_run:
            logger.info(f"Dry run: would post in chat: {text}")
            return
        try:
            client = self.publisher._youtube_factory()
            video_id = await self._client_call(self._video_id, client)
            chat_id = await self._client_call(client.active_live_chat_id, video_id) if video_id else ""
            if chat_id:
                await self._client_call(client.post_chat_message, chat_id, text)
        except Exception as exc:
            logger.debug(f"Goodbye chat message failed: {exc}")
