"""Clip markers: the best moments of a live stream, ready to cut.

Anything worth a short clip (the network guessing wrong, a finished build,
chat going wild) calls ``CLIPS.mark(...)``. Each mark shows up in the
terminal right away as a ✂ CLIP line with its time in the stream, and when
the server stops (Ctrl+C) the best ones are printed as ~45 second windows
with links that open the YouTube video at that moment (they work once the
stream is saved as a video). The same list is saved in logs/clips-*.txt.

Times are counted from when YouTube says the stream went live (the stream
autopilot reports it), or from when OBS started streaming before that.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from loguru import logger

BEFORE = 20.0  # a clip starts this long before the moment (the build-up)
AFTER = 25.0  # and ends this long after it (the reaction): 45 s in all
MAX_CLIP = 58.0  # moments close together merge into one clip, never past this
TOP = 12  # clips listed at the end
LOG_DIR = Path("logs")


@dataclass
class Mark:
    at: float  # wall clock
    kind: str
    text: str
    weight: float


def stamp(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


class ClipMarks:
    def __init__(self) -> None:
        self.marks: list[Mark] = []
        self.live_since: Optional[float] = None
        self.video_id = ""
        self._exact = False  # live_since comes from YouTube itself
        self._listeners: list[Callable[[Mark], None]] = []

    def add_listener(self, listener: Callable[[Mark], None]) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def set_live(
        self, started_at: float, video_id: str = "", exact: bool = True
    ) -> None:
        """When the stream went live (YouTube's time wins over OBS's guess)."""
        if (
            started_at
            and (exact or self.live_since is None)
            and not (self._exact and not exact)
        ):
            self.live_since = started_at
            self._exact = self._exact or exact
        if video_id:
            self.video_id = video_id

    def offset(self, at: float) -> float:
        return at - (
            self.live_since or (self.marks[0].at - BEFORE if self.marks else at)
        )

    def mark(
        self, kind: str, text: str, weight: float = 1.0, at: Optional[float] = None
    ) -> None:
        at = at or time.time()
        self.marks.append(Mark(at, kind, text.strip()[:120], weight))
        mark = self.marks[-1]
        when = (
            stamp(self.offset(at))
            if self.live_since
            else time.strftime("%H:%M:%S", time.localtime(at))
        )
        logger.info(f"✂ CLIP {when}  {kind}: {text[:100]}")
        for listener in list(self._listeners):
            try:
                listener(mark)
            except Exception as exc:
                logger.debug(f"Clip listener failed: {exc}")

    def clips(self) -> list[dict]:
        """Moments merged into clips (45 s around each, close ones together),
        best first, at most TOP."""
        out: list[dict] = []
        for m in sorted(self.marks, key=lambda m: m.at):
            start, end = m.at - BEFORE, m.at + AFTER
            if (
                out
                and start <= out[-1]["end"]
                and max(end, out[-1]["end"]) - out[-1]["start"] <= MAX_CLIP
            ):
                clip = out[-1]
                clip["end"] = max(clip["end"], end)
                clip["score"] += m.weight
                clip["moments"].append(m)
                continue
            out.append({"start": start, "end": end, "score": m.weight, "moments": [m]})
        out.sort(key=lambda c: -c["score"])
        return out[:TOP]

    def report(self) -> str:
        clips = self.clips()
        if not clips:
            return ""
        lines = [
            "",
            "✂ ✂ ✂  Clip moments from this stream (about 45 s each, best first)  ✂ ✂ ✂",
        ]
        if not self.live_since:
            lines.append(
                "(the stream never went live: times are the clock on this computer)"
            )
        for i, clip in enumerate(clips, 1):
            if self.live_since:
                a, b = self.offset(clip["start"]), self.offset(clip["end"])
                span = f"{stamp(a)} - {stamp(b)}"
            else:
                span = (
                    time.strftime("%H:%M:%S", time.localtime(clip["start"]))
                    + " - "
                    + time.strftime("%H:%M:%S", time.localtime(clip["end"]))
                )
            what = " / ".join(m.text for m in clip["moments"][:3])
            lines.append(f"{i:>2}. {span}  {what}")
            if self.video_id and self.live_since:
                lines.append(
                    f"    https://youtu.be/{self.video_id}?t={max(0, int(self.offset(clip['start'])))}"
                )
        lines.append(
            "Cut them in YouTube Studio (Editor, or Create clip on the video), or any editor."
        )
        return "\n".join(lines)

    def save(self) -> Optional[Path]:
        text = self.report()
        if not text:
            return None
        try:
            LOG_DIR.mkdir(exist_ok=True)
            path = LOG_DIR / time.strftime(
                "clips-%Y-%m-%d-%H%M.txt", time.localtime(self.marks[0].at)
            )
            path.write_text(text.lstrip() + "\n", encoding="utf-8")
            return path
        except Exception as exc:
            logger.debug(f"Clip list not saved: {exc}")
            return None

    def finish(self) -> None:
        """At the very end (after Ctrl+C): print the list and save it."""
        text = self.report()
        if not text:
            return
        path = self.save()
        print(text + (f"\nSaved to {path}" if path else ""), flush=True)


CLIPS = ClipMarks()
