"""Local 9:16 Shorts production from OBS recordings."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

CANVAS_W = 1080
CANVAS_H = 1920
VIDEO_X = 40
VIDEO_Y = 615
VIDEO_W = 1000
VIDEO_H = 563


def safe_slug(value: str) -> str:
    value = str(value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value[:100] or "clip"


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"{name} not found in PATH")
    return path


def make_short(
    source_clip: str | Path,
    event: str,
    request: str = "",
    viewer: str = "",
    started_at: float | None = None,
    finished_at: float | None = None,
    output_dir: str | Path = "shorts",
    background: str | Path = "Clipbg.png",
) -> tuple[Path, Path]:
    """Render a 1080x1920 MP4 Short and a JSON sidecar.

    The horizontal clip is letterboxed inside the central frame so Minecraft,
    Mika and Luna are preserved instead of cropped away.
    """
    require_binary("ffmpeg")
    source = Path(source_clip)
    if not source.is_file():
        raise RuntimeError(f"source clip not found: {source}")
    bg = Path(background)
    if not bg.is_file():
        raise RuntimeError(f"Clip background not found: {bg}")
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d-%H%M%S", time.localtime(started_at or time.time()))
    slug = safe_slug("-".join(x for x in [event, viewer, request] if x))
    output = out_dir / f"{stamp}-{slug}.mp4"
    sidecar = output.with_suffix(".json")
    filter_complex = (
        f"[0:v]scale={CANVAS_W}:{CANVAS_H}:force_original_aspect_ratio=increase,"
        f"crop={CANVAS_W}:{CANVAS_H}[bg];"
        f"[1:v]scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,"
        f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2:black[clip];"
        f"[bg][clip]overlay={VIDEO_X}:{VIDEO_Y}:format=auto,format=yuv420p[final]"
    )
    command = [
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-i",
        str(bg),
        "-i",
        str(source),
        "-filter_complex",
        filter_complex,
        "-map",
        "[final]",
        "-map",
        "1:a?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        "-ar",
        "44100",
        "-movflags",
        "+faststart",
        "-shortest",
        str(output),
    ]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-4000:] or result.stdout[-4000:] or "ffmpeg failed")
    if not output.exists():
        raise RuntimeError("Short output was not created")
    metadata: dict[str, Any] = {
        "event": event,
        "viewer": viewer,
        "request": request,
        "started_at": started_at,
        "finished_at": finished_at,
        "source_clip": str(source),
        "short_file": str(output),
    }
    sidecar.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return output, sidecar
