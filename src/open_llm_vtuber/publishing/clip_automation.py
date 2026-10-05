"""OBS clip recording and post-processing for livestream moments."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from . import obs_control
from .shorts import safe_slug


def _flag(name: str, default: str = "0") -> bool:
    if name == "CLIP_AUTO_RECORD_ENABLED" and name not in os.environ and os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float, low: float = 0.0, high: float = 3600.0) -> float:
    try:
        return max(low, min(high, float(os.environ.get(name, "") or default)))
    except ValueError:
        return default


def _kinds() -> set[str]:
    raw = os.environ.get(
        "CLIP_AUTO_RECORD_KINDS",
        "race,tnt,potion,spell,built,build_request,done,wrong,reaction,superchat,chat",
    )
    return {x.strip().lower() for x in raw.split(",") if x.strip()}


@dataclass
class ClipEvent:
    kind: str
    text: str
    viewer: str = ""
    request: str = ""
    at: float = field(default_factory=time.time)


@dataclass
class ClipSession:
    started_at: float
    event: ClipEvent
    events: list[ClipEvent] = field(default_factory=list)
    source_clip: Optional[Path] = None


class AutoClipRecorder:
    """One OBS recording at a time; overlapping events extend the tail."""

    def __init__(self) -> None:
        self.enabled = _flag("CLIP_AUTO_RECORD_ENABLED", "1")
        self.tail_seconds = _float("CLIP_TAIL_SECONDS", 3.0, 0.0, 20.0)
        self.safety_delay = _float("CLIP_START_SAFETY_DELAY", 0.3, 0.0, 2.0)
        self.initial_hold = _float("CLIP_INITIAL_HOLD_SECONDS", 90.0, 5.0, 1800.0)
        self.output_dir = Path(os.environ.get("CLIPS_DIR", "clips"))
        self.kinds = _kinds()
        self._lock = asyncio.Lock()
        self._session: Optional[ClipSession] = None
        self._tail_deadline = 0.0
        self._monitor: Optional[asyncio.Task] = None

    def wants(self, kind: str, weight: float = 0.0) -> bool:
        return self.enabled and (kind.lower() in self.kinds or weight >= 2.5)

    async def start_or_extend(
        self,
        kind: str,
        text: str,
        viewer: str = "",
        request: str = "",
        at: float | None = None,
        initial_hold: float | None = None,
    ) -> bool:
        if not self.enabled:
            return False
        event = ClipEvent(kind=kind, text=text[:180], viewer=viewer[:80], request=request[:220], at=at or time.time())
        async with self._lock:
            now = time.time()
            if self._session is not None:
                self._session.events.append(event)
                self._tail_deadline = max(self._tail_deadline, now + self.tail_seconds)
                logger.info(f"OBS clip: extended recording for {kind}: {text[:80]}")
                return True
            try:
                await obs_control.start_recording()
            except Exception as exc:
                logger.warning(f"OBS clip: recording did not start ({exc})")
                return False
            self._session = ClipSession(started_at=now, event=event, events=[event])
            self._tail_deadline = now + (initial_hold if initial_hold is not None else self.initial_hold)
            logger.info(f"OBS clip: recording started for {kind}: {text[:80]}")
            if self._monitor is None or self._monitor.done():
                self._monitor = asyncio.create_task(self._monitor_tail(), name="obs-clip-recorder")
            return True

    async def finish_after_tail(self, kind: str = "", text: str = "") -> None:
        if not self.enabled:
            return
        async with self._lock:
            if self._session is None:
                return
            if kind or text:
                self._session.events.append(ClipEvent(kind=kind or "tail", text=text[:180]))
            self._tail_deadline = max(self._tail_deadline, time.time() + self.tail_seconds)

    async def _monitor_tail(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            async with self._lock:
                if self._session is None:
                    return
                wait = self._tail_deadline - time.time()
            if wait > 0:
                continue
            await self._stop_current()

    async def _stop_current(self) -> None:
        async with self._lock:
            session = self._session
            self._session = None
        if session is None:
            return
        try:
            output = await obs_control.stop_recording()
        except Exception as exc:
            logger.warning(f"OBS clip: recording did not stop cleanly ({exc})")
            return
        if output is None:
            logger.warning("OBS clip: OBS stopped recording but did not report a file")
            return
        session.source_clip = output
        finalized = await asyncio.to_thread(self._finalize_clip, session)
        if finalized:
            logger.info(f"OBS clip: finalized source clip {finalized}")

    def _finalize_clip(self, session: ClipSession) -> Optional[Path]:
        if session.source_clip is None:
            return None
        source = Path(session.source_clip)
        if not self._wait_for_final_file(source):
            logger.warning(f"OBS clip: recording file is not finalized/readable: {source}")
            return None
        event = session.event
        self.output_dir.mkdir(parents=True, exist_ok=True)
        ext = source.suffix or ".mp4"
        stamp = time.strftime("%Y-%m-%d-%H%M%S", time.localtime(session.started_at))
        target = self.output_dir / f"{stamp}-{safe_slug(event.kind + '-' + (event.viewer or event.text))}{ext}"
        n = 2
        while target.exists():
            target = self.output_dir / f"{stamp}-{safe_slug(event.kind + '-' + (event.viewer or event.text))}-{n}{ext}"
            n += 1
        try:
            shutil.copy2(source, target)
        except Exception as exc:
            logger.warning(f"OBS clip: could not copy finalized source clip: {exc}")
            return None
        if not self._ffprobe_ok(target):
            try:
                target.unlink(missing_ok=True)
            except Exception:
                pass
            logger.warning(f"OBS clip: copied clip failed ffprobe, removed: {target}")
            return None
        return target

    def _wait_for_final_file(self, path: Path) -> bool:
        checks = int(_float("CLIP_FINALIZE_STABLE_CHECKS", 3, 1, 20))
        delay = _float("CLIP_FINALIZE_CHECK_SECONDS", 1.0, 0.1, 10.0)
        deadline = time.time() + _float("CLIP_FINALIZE_TIMEOUT_SECONDS", 60.0, 5.0, 600.0)
        stable = 0
        last = -1
        while time.time() < deadline:
            if not path.exists():
                time.sleep(delay)
                continue
            size = path.stat().st_size
            if size > 0 and size == last:
                stable += 1
            else:
                stable = 0
            last = size
            if stable >= checks and self._ffprobe_ok(path):
                return True
            time.sleep(delay)
        return False

    def _ffprobe_ok(self, path: Path) -> bool:
        if not shutil.which("ffprobe"):
            logger.warning("OBS clip: ffprobe not found; cannot finalize clip safely")
            return False
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        return result.returncode == 0 and "codec_type" in result.stdout


class SocialScreenshotter:
    def __init__(self) -> None:
        self.enabled = _flag("OBS_SOCIAL_SCREENSHOT_ENABLED", "1")
        self.output_dir = Path(os.environ.get("SOCIAL_SCREENSHOT_DIR", "social"))

    async def capture(self, title: str, source_name: str = "") -> Optional[Path]:
        if not self.enabled:
            return None
        stamp = time.strftime("%Y-%m-%d-%H%M%S")
        path = self.output_dir / f"{safe_slug(title)}-{stamp}.jpg"
        try:
            await obs_control.capture_obs_frame(1920, 1080, 90, save_path=path, source_name=source_name)
            logger.info(f"OBS social screenshot: saved {path}")
            return path
        except Exception as exc:
            logger.warning(f"OBS social screenshot failed: {exc}")
            return None


RECORDER = AutoClipRecorder()
SCREENSHOTS = SocialScreenshotter()
_STARTED = False


async def start_clip_event(kind: str, text: str, viewer: str = "", request: str = "") -> bool:
    if os.environ.get("PYTEST_CURRENT_TEST") and "CLIP_AUTO_RECORD_ENABLED" not in os.environ:
        return False
    return await RECORDER.start_or_extend(kind, text, viewer=viewer, request=request)


def install_clip_automation() -> None:
    """Subscribe OBS recording/social capture to existing clip markers."""
    global _STARTED
    if _STARTED:
        return
    _STARTED = True
    from ..live.clip_marks import CLIPS

    def listener(mark: Any) -> None:
        if RECORDER.wants(mark.kind, mark.weight):
            asyncio.create_task(RECORDER.start_or_extend(mark.kind, mark.text, at=mark.at))
            asyncio.create_task(RECORDER.finish_after_tail(mark.kind, mark.text))
        if mark.kind == "built" and "complete" in mark.text.lower():
            asyncio.create_task(SCREENSHOTS.capture(mark.text))

    CLIPS.add_listener(listener)
    logger.info(
        "OBS clip automation: "
        + ("on" if RECORDER.enabled else "off")
        + f", kinds={','.join(sorted(RECORDER.kinds))}"
    )
