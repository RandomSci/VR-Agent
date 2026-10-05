"""Local OBS-frame Minecraft visual health checks and recovery helpers."""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np
from loguru import logger


class RecoveryStep(str, Enum):
    HEALTHY = "healthy"
    FOCUS_CLICK = "focus_click"
    CLIENT_RELOAD = "client_reload"
    BACKUP = "backup"
    CLIENT_RESTART = "client_restart"


@dataclass
class HealthState:
    server_healthy: bool = False
    agents_healthy: bool = False
    minecraft_client_healthy: bool = False
    visual_healthy: bool = True
    stream_healthy: bool = True

    def as_dict(self) -> dict[str, bool]:
        return {
            "SERVER_HEALTHY": self.server_healthy,
            "AGENTS_HEALTHY": self.agents_healthy,
            "MINECRAFT_CLIENT_HEALTHY": self.minecraft_client_healthy,
            "VISUAL_HEALTHY": self.visual_healthy,
            "STREAM_HEALTHY": self.stream_healthy,
        }


@dataclass
class VisualSample:
    score: float
    strikes: int
    unhealthy: bool
    healthy: bool


def _crop(gray: np.ndarray, crop: tuple[float, float, float, float]) -> np.ndarray:
    h, w = gray.shape[:2]
    left, top, right, bottom = crop
    x1 = int(w * left)
    y1 = int(h * top)
    x2 = max(x1 + 1, int(w * (1.0 - right)))
    y2 = max(y1 + 1, int(h * (1.0 - bottom)))
    return gray[y1:y2, x1:x2]


def parse_crop(value: str = "") -> tuple[float, float, float, float]:
    if not value:
        return (0.08, 0.06, 0.08, 0.18)
    try:
        parts = [max(0.0, min(0.45, float(x.strip()))) for x in value.split(",")]
    except ValueError:
        return (0.08, 0.06, 0.08, 0.18)
    return tuple(parts[:4]) if len(parts) == 4 else (0.08, 0.06, 0.08, 0.18)


def decode_gray(image: bytes, crop: tuple[float, float, float, float] | None = None) -> np.ndarray:
    """Decode a JPEG/PNG screenshot to cropped uint8 grayscale."""
    try:
        import cv2  # type: ignore

        arr = np.frombuffer(image, dtype=np.uint8)
        gray = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if gray is None:
            raise ValueError("cv2 could not decode image")
    except Exception:
        from PIL import Image

        with Image.open(io.BytesIO(image)) as img:
            gray = np.asarray(img.convert("L"), dtype=np.uint8)
    return _crop(gray, crop) if crop else gray


def diff_score(previous: np.ndarray, current: np.ndarray) -> float:
    if previous.shape != current.shape:
        h = min(previous.shape[0], current.shape[0])
        w = min(previous.shape[1], current.shape[1])
        previous = previous[:h, :w]
        current = current[:h, :w]
    return float(np.mean(np.abs(current.astype(np.int16) - previous.astype(np.int16))))


class VisualFreezeDetector:
    def __init__(self, threshold: float, strikes_required: int) -> None:
        self.threshold = threshold
        self.strikes_required = strikes_required
        self.previous: Optional[np.ndarray] = None
        self.strikes = 0

    def reset(self) -> None:
        self.previous = None
        self.strikes = 0

    def update(self, frame: np.ndarray, expect_activity: bool = True) -> VisualSample:
        if self.previous is None:
            self.previous = frame
            self.strikes = 0
            return VisualSample(0.0, 0, False, True)
        score = diff_score(self.previous, frame)
        self.previous = frame
        if expect_activity and score < self.threshold:
            self.strikes += 1
        else:
            self.strikes = 0
        unhealthy = self.strikes >= self.strikes_required
        return VisualSample(score, self.strikes, unhealthy, not unhealthy)


class RecoveryStateMachine:
    def __init__(self, backup_after_failures: int = 2) -> None:
        self.backup_after_failures = max(1, backup_after_failures)
        self.failures = 0
        self.step = RecoveryStep.HEALTHY
        self.in_backup = False

    def visual_failed(self) -> RecoveryStep:
        self.step = RecoveryStep.FOCUS_CLICK
        return self.step

    def focus_result(self, recovered: bool) -> RecoveryStep:
        if recovered:
            return self.recovered()
        self.failures += 1
        self.step = RecoveryStep.CLIENT_RELOAD
        return self.step

    def reload_result(self, recovered: bool) -> RecoveryStep:
        if recovered:
            return self.recovered()
        self.failures += 1
        if self.failures >= self.backup_after_failures:
            self.in_backup = True
            self.step = RecoveryStep.BACKUP
        else:
            self.step = RecoveryStep.FOCUS_CLICK
        return self.step

    def backup_failed(self) -> RecoveryStep:
        self.step = RecoveryStep.CLIENT_RESTART
        return self.step

    def recovered(self) -> RecoveryStep:
        self.failures = 0
        self.step = RecoveryStep.HEALTHY
        return self.step


async def _run_tool(args: list[str], timeout: float = 4.0) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(
        subprocess.run,
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


async def minecraft_window_exists() -> bool:
    if not shutil.which("xdotool"):
        return False
    result = await _run_tool(["xdotool", "search", "--onlyvisible", "--name", "Minecraft"], timeout=2.0)
    return result.returncode == 0 and bool(result.stdout.strip())


async def focus_and_click_minecraft() -> bool:
    if not shutil.which("xdotool"):
        logger.warning("WATCHDOG xdotool not found; cannot focus Minecraft window")
        return False
    result = await _run_tool(["xdotool", "search", "--onlyvisible", "--name", "Minecraft"], timeout=2.0)
    windows = [w for w in result.stdout.splitlines() if w.strip()]
    if not windows:
        logger.warning("WATCHDOG Minecraft window not found")
        return False
    wid = windows[-1].strip()
    await _run_tool(["xdotool", "windowactivate", "--sync", wid], timeout=3.0)
    await _run_tool(["xdotool", "windowfocus", wid], timeout=2.0)
    await _run_tool(["xdotool", "mousemove", "--window", wid, "80", "80", "click", "1"], timeout=3.0)
    return True


async def reload_minecraft_chunks() -> bool:
    if not shutil.which("xdotool"):
        return False
    if not await focus_and_click_minecraft():
        return False
    # F3+A is Minecraft's deterministic client chunk reload shortcut.
    await _run_tool(["xdotool", "key", "F3+a"], timeout=3.0)
    return True


async def restart_minecraft_client_from_env() -> bool:
    command = os.environ.get("MINECRAFT_CLIENT_RESTART_COMMAND", "").strip()
    if not command:
        return False
    logger.warning("WATCHDOG restarting Minecraft client with MINECRAFT_CLIENT_RESTART_COMMAND")
    proc = await asyncio.create_subprocess_shell(command)
    try:
        await asyncio.wait_for(proc.wait(), timeout=20)
    except asyncio.TimeoutError:
        return True
    return proc.returncode == 0


def watchdog_enabled() -> bool:
    return os.environ.get("MINECRAFT_WATCHDOG_ENABLED", "1").strip().lower() not in ("0", "false", "off", "no")
