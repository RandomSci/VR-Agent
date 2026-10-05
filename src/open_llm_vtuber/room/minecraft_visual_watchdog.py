"""Local OBS-frame Minecraft visual health checks and recovery helpers."""

from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import subprocess
import zlib
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np
from loguru import logger

_LAUNCH_LOCK: Optional[asyncio.Lock] = None


class RecoveryStep(str, Enum):
    HEALTHY = "healthy"
    FOCUS_CLICK = "focus_click"
    DISCONNECT_RECONNECT = "disconnect_reconnect"
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
        return (0.25, 0.25, 0.25, 0.25)
    try:
        parts = [max(0.0, min(0.45, float(x.strip()))) for x in value.split(",")]
    except ValueError:
        return (0.25, 0.25, 0.25, 0.25)
    return tuple(parts[:4]) if len(parts) == 4 else (0.25, 0.25, 0.25, 0.25)


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
        self.previous_checksum = 0
        self.previous_mean = 0.0
        self.strikes = 0

    def reset(self) -> None:
        self.previous = None
        self.previous_checksum = 0
        self.previous_mean = 0.0
        self.strikes = 0

    @staticmethod
    def frame_checksum(frame: np.ndarray) -> int:
        return int(zlib.adler32(np.ascontiguousarray(frame).tobytes()))

    def update(self, frame: np.ndarray, expect_activity: bool = True) -> VisualSample:
        checksum = self.frame_checksum(frame)
        mean = float(frame.mean())
        if self.previous is None:
            self.previous = frame
            self.previous_checksum = checksum
            self.previous_mean = mean
            self.strikes = 0
            return VisualSample(0.0, 0, False, True)
        score = diff_score(self.previous, frame)
        self.previous = frame
        checksum_changed = checksum != self.previous_checksum
        mean_changed = abs(mean - self.previous_mean) >= max(0.02, self.threshold * 0.02)
        static_score = score <= max(0.03, self.threshold * 0.05)
        truly_static = static_score and not checksum_changed and not mean_changed
        self.previous_checksum = checksum
        self.previous_mean = mean
        if expect_activity and truly_static:
            self.strikes += 1
        elif expect_activity and score < self.threshold and self.strikes:
            self.strikes = max(0, self.strikes - 1)
        else:
            self.strikes = 0
        unhealthy = self.strikes >= self.strikes_required
        return VisualSample(score, self.strikes, unhealthy, not unhealthy)


class RecoveryStateMachine:
    def __init__(self, backup_after_failures: int = 2, cooldown_seconds: float = 60.0) -> None:
        self.backup_after_failures = max(1, backup_after_failures)
        self.cooldown_seconds = max(0.0, cooldown_seconds)
        self.failures = 0
        self.step = RecoveryStep.HEALTHY
        self.in_backup = False
        self.in_progress = False
        self.cooldown_until = 0.0

    def can_start(self, now: float) -> bool:
        return not self.in_progress and now >= self.cooldown_until

    def start_recovery(self, now: float) -> bool:
        if not self.can_start(now):
            return False
        self.in_progress = True
        self.visual_failed()
        return True

    def finish_recovery(self, success: bool, now: float) -> None:
        self.in_progress = False
        if success:
            self.recovered()
        else:
            self.cooldown_until = now + self.cooldown_seconds

    def visual_failed(self) -> RecoveryStep:
        self.step = RecoveryStep.FOCUS_CLICK
        return self.step

    def focus_result(self, recovered: bool) -> RecoveryStep:
        if recovered:
            return self.recovered()
        self.failures += 1
        self.step = RecoveryStep.DISCONNECT_RECONNECT
        return self.step

    def reconnect_result(self, recovered: bool) -> RecoveryStep:
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
        self.in_backup = False
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


def _launch_lock() -> asyncio.Lock:
    global _LAUNCH_LOCK
    if _LAUNCH_LOCK is None:
        _LAUNCH_LOCK = asyncio.Lock()
    return _LAUNCH_LOCK


def _env_float(name: str, default: float, low: float = 0.0, high: float = 10_000.0) -> float:
    try:
        return max(low, min(high, float(os.environ.get(name, "") or default)))
    except ValueError:
        return default


def _env_int(name: str, default: int, low: int = 0, high: int = 10_000) -> int:
    try:
        return max(low, min(high, int(float(os.environ.get(name, "") or default))))
    except ValueError:
        return default


def minecraft_game_pattern() -> str:
    return os.environ.get("MINECRAFT_GAME_WINDOW_PATTERN", r"Minecraft 1\.21\.6").strip() or r"Minecraft 1\.21\.6"


def launcher_pattern() -> str:
    return os.environ.get("MINECRAFT_LAUNCHER_WINDOW_PATTERN", r"^Minecraft Launcher$").strip() or r"^Minecraft Launcher$"


def launcher_command() -> str:
    return os.path.expanduser(
        os.environ.get("MINECRAFT_LAUNCHER_COMMAND", "~/Applications/minecraft-launcher/minecraft-launcher").strip()
    )


async def _search_visible(pattern: str, timeout: float = 2.0) -> list[str]:
    if not shutil.which("xdotool"):
        return []
    result = await _run_tool(["xdotool", "search", "--onlyvisible", "--name", pattern], timeout=timeout)
    if result.returncode != 0:
        return []
    return [w.strip() for w in result.stdout.splitlines() if w.strip()]


async def _window_name(window_id: str) -> str:
    result = await _run_tool(["xdotool", "getwindowname", window_id], timeout=2.0)
    return result.stdout.strip() if result.returncode == 0 else ""


async def active_window_is_minecraft() -> bool:
    if not shutil.which("xdotool"):
        return False
    result = await _run_tool(["xdotool", "getactivewindow"], timeout=2.0)
    window_id = result.stdout.strip() if result.returncode == 0 else ""
    if not window_id:
        return False
    title = await _window_name(window_id)
    return bool(re.search(minecraft_game_pattern(), title) and "launcher" not in title.lower())


async def _activate_window(window_id: str, label: str = "game", attempts: int = 10) -> bool:
    for attempt in range(1, attempts + 1):
        if label == "game":
            window_id = await find_minecraft_game_window() or window_id
        if not window_id:
            logger.warning(f"WATCHDOG {label} activation failed: no window; retrying")
            await asyncio.sleep(0.5)
            continue
        logger.warning(f"WATCHDOG {label} activation attempt {attempt}/{attempts}")
        try:
            result = await _run_tool(["xdotool", "windowactivate", window_id], timeout=3.0)
            if result.returncode == 0:
                await _run_tool(["xdotool", "windowfocus", window_id], timeout=2.0)
                await asyncio.sleep(0.25)
                if label != "game" or await active_window_is_minecraft():
                    return True
            stderr = getattr(result, "stderr", "") or ""
            if "BadWindow" in stderr:
                logger.warning(f"WATCHDOG {label} activation BadWindow; reacquiring")
        except (subprocess.TimeoutExpired, asyncio.TimeoutError, TimeoutError) as exc:
            logger.warning(f"WATCHDOG {label} activation timed out ({exc}); retrying")
        except Exception as exc:
            logger.warning(f"WATCHDOG {label} activation failed ({exc}); retrying")
        logger.warning(f"WATCHDOG {label} activation failed; retrying")
        await asyncio.sleep(0.5)
    return False


async def find_minecraft_game_window() -> Optional[str]:
    """Find the real Minecraft game window, not the launcher."""
    for window_id in reversed(await _search_visible(minecraft_game_pattern())):
        name = await _window_name(window_id)
        if "launcher" not in name.lower():
            return window_id
    return None


async def find_launcher_window() -> Optional[str]:
    windows = await _search_visible(launcher_pattern())
    return windows[-1] if windows else None


async def focus_minecraft() -> Optional[str]:
    window_id = await find_minecraft_game_window()
    if not window_id:
        return None
    return window_id if await _activate_window(window_id, "game") else None


async def verify_multiplayer_connected() -> bool:
    window_id = await find_minecraft_game_window()
    if not window_id:
        return False
    title = await _window_name(window_id)
    return "multiplayer" in title.lower()


async def minecraft_process_exists() -> bool:
    """Best-effort hard health signal for the graphical Java client."""
    if not shutil.which("pgrep"):
        return await find_minecraft_game_window() is not None
    patterns = (
        os.environ.get("MINECRAFT_PROCESS_PATTERN", "").strip(),
        "Minecraft 1.21.6",
        "minecraft.*client",
        "net.minecraft.client",
    )
    for pattern in [p for p in patterns if p]:
        result = await _run_tool(["pgrep", "-f", pattern], timeout=2.0)
        if result.returncode == 0 and result.stdout.strip():
            return True
    return False


async def minecraft_hard_health() -> dict[str, bool]:
    window = await find_minecraft_game_window() is not None
    multiplayer = await verify_multiplayer_connected() if window else False
    process = await minecraft_process_exists() or window
    return {
        "process_alive": process,
        "game_window_exists": window,
        "multiplayer_connected": multiplayer,
    }


async def _wait_for_game_window(timeout: float | None = None) -> Optional[str]:
    end = asyncio.get_running_loop().time() + (timeout if timeout is not None else _env_float("MINECRAFT_GAME_WINDOW_TIMEOUT", 180.0))
    while asyncio.get_running_loop().time() < end:
        window_id = await find_minecraft_game_window()
        if window_id:
            return window_id
        await asyncio.sleep(2.0)
    return None


async def _wait_for_launcher(timeout: float | None = None) -> Optional[str]:
    end = asyncio.get_running_loop().time() + (timeout if timeout is not None else _env_float("MINECRAFT_LAUNCHER_TIMEOUT", 90.0))
    while asyncio.get_running_loop().time() < end:
        window_id = await find_launcher_window()
        if window_id:
            return window_id
        await asyncio.sleep(1.0)
    return None


async def _click_launcher_play(window_id: str) -> bool:
    readiness = _env_float("MINECRAFT_LAUNCHER_READY_SECONDS", 15.0, 0.0, 120.0)
    if readiness:
        await asyncio.sleep(readiness)
    result = await _run_tool(["xdotool", "getwindowgeometry", "--shell", window_id], timeout=3.0)
    if result.returncode != 0:
        return False
    values: dict[str, int] = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key in ("WIDTH", "HEIGHT"):
            try:
                values[key] = int(value)
            except ValueError:
                pass
    width, height = values.get("WIDTH", 1280), values.get("HEIGHT", 720)
    x = width * _env_int("MINECRAFT_LAUNCHER_PLAY_X_PERCENT", 54, 0, 100) // 100
    y = height * _env_int("MINECRAFT_LAUNCHER_PLAY_Y_PERCENT", 74, 0, 100) // 100
    await _run_tool(["xdotool", "mousemove", "--window", window_id, str(x), str(y)], timeout=3.0)
    await _run_tool(["xdotool", "click", "1"], timeout=3.0)
    logger.warning("WATCHDOG launcher PLAY clicked")
    return True


async def confirm_multiplayer_menu(window_id: str) -> bool:
    # Minecraft's window title does not distinguish main menu from the server list.
    # With xdotool-only control, the bounded activation/open sequence is the local
    # deterministic confirmation before clicking the saved server row.
    return bool(window_id and await find_minecraft_game_window()) and not await verify_multiplayer_connected()


async def open_multiplayer_screen(window_id: str = "") -> bool:
    for attempt in range(1, _env_int("MINECRAFT_MULTIPLAYER_KEY_ATTEMPTS", 5, 1, 20) + 1):
        window_id = await find_minecraft_game_window() or window_id
        if not window_id:
            return False
        if not await _activate_window(window_id, "game"):
            continue
        await _run_tool(["xdotool", "key", "Escape"], timeout=3.0)
        await asyncio.sleep(1.0)
        if attempt == 1:
            logger.warning("WATCHDOG Multiplayer selected")
            for key in ("Tab", "Down"):
                await _run_tool(["xdotool", "key", key], timeout=3.0)
                await asyncio.sleep(0.3)
        logger.warning(f"WATCHDOG activating Multiplayer attempt {attempt}/5")
        await _run_tool(["xdotool", "key", "Return"], timeout=3.0)
        await asyncio.sleep(_env_float("MINECRAFT_MULTIPLAYER_MENU_READY_SECONDS", 1.0, 0.0, 10.0))
        if await confirm_multiplayer_menu(window_id):
            logger.warning("WATCHDOG Multiplayer menu confirmed")
            return True
    window_id = await find_minecraft_game_window() or window_id
    if not window_id or not await _activate_window(window_id, "game"):
        return False
    logger.warning("WATCHDOG keyboard Multiplayer activation failed; using mouse fallback")
    x = _env_int("MINECRAFT_MULTIPLAYER_BUTTON_X", 960, 0, 4000)
    y = _env_int("MINECRAFT_MULTIPLAYER_BUTTON_Y", 510, 0, 4000)
    await _run_tool(["xdotool", "mousemove", "--window", window_id, str(x), str(y)], timeout=3.0)
    await _run_tool(["xdotool", "click", "1"], timeout=3.0)
    await asyncio.sleep(_env_float("MINECRAFT_MULTIPLAYER_MENU_READY_SECONDS", 1.0, 0.0, 10.0))
    if await confirm_multiplayer_menu(window_id):
        logger.warning("WATCHDOG Multiplayer menu confirmed")
        return True
    return False


async def join_saved_server(window_id: str) -> bool:
    if not await confirm_multiplayer_menu(window_id):
        logger.warning("WATCHDOG saved server join skipped: Multiplayer menu not confirmed")
        return False
    x = _env_int("MINECRAFT_SERVER_ROW_X", 800, 0, 4000)
    y = _env_int("MINECRAFT_SERVER_ROW_Y", 170, 0, 4000)
    if not await _activate_window(window_id, "game"):
        return False
    logger.warning("WATCHDOG joining saved server")
    await _run_tool(["xdotool", "mousemove", "--window", window_id, str(x), str(y)], timeout=3.0)
    await _run_tool(["xdotool", "click", "--repeat", "2", "--delay", "180", "1"], timeout=3.0)
    return await wait_for_multiplayer_connected()


async def wait_for_multiplayer_connected(timeout: float | None = None) -> bool:
    end = asyncio.get_running_loop().time() + (timeout if timeout is not None else _env_float("MINECRAFT_RECONNECT_TIMEOUT", 90.0))
    while asyncio.get_running_loop().time() < end:
        if await verify_multiplayer_connected():
            logger.warning("WATCHDOG multiplayer title detected")
            return True
        await asyncio.sleep(2.0)
    return False


async def disconnect_minecraft_client() -> bool:
    window_id = await focus_minecraft()
    if not window_id:
        logger.warning("WATCHDOG disconnect failed: Minecraft game window not found")
        return False
    await _run_tool(["xdotool", "key", "Escape"], timeout=3.0)
    await asyncio.sleep(_env_float("MINECRAFT_DISCONNECT_MENU_DELAY", 1.0, 0.0, 10.0))
    x = _env_int("MINECRAFT_DISCONNECT_X", 960, 0, 4000)
    y = _env_int("MINECRAFT_DISCONNECT_Y", 735, 0, 4000)
    await _run_tool(["xdotool", "mousemove", "--window", window_id, str(x), str(y)], timeout=3.0)
    await _run_tool(["xdotool", "click", "1"], timeout=3.0)
    logger.warning("WATCHDOG disconnect clicked")
    await asyncio.sleep(_env_float("MINECRAFT_AFTER_DISCONNECT_SECONDS", 3.0, 0.0, 30.0))
    return True


async def reconnect_minecraft_client() -> bool:
    if await verify_multiplayer_connected():
        return True
    window_id = await find_minecraft_game_window()
    if not window_id:
        return False
    logger.warning("WATCHDOG reconnect attempt: joining saved local server")
    if await join_saved_server(window_id):
        return True
    return False


async def disconnect_reconnect_minecraft_client() -> bool:
    logger.warning("WATCHDOG disconnect recovery starting")
    if not await disconnect_minecraft_client():
        return False
    ok = await reconnect_minecraft_client()
    logger.warning("WATCHDOG disconnect successful" if ok else "WATCHDOG reconnect timeout")
    return ok


async def launch_minecraft_client() -> bool:
    lock = _launch_lock()
    if lock.locked():
        logger.warning("WATCHDOG launch skipped: launch already in progress")
        return False
    async with lock:
        if await verify_multiplayer_connected():
            return True
        if await find_minecraft_game_window() is not None:
            logger.warning("WATCHDOG launch skipped: Minecraft game window already exists")
            return False
        if await minecraft_process_exists():
            logger.warning("WATCHDOG launch skipped: Minecraft process already exists")
            return False
        if not shutil.which("xdotool"):
            logger.warning("WATCHDOG xdotool not found; cannot launch graphical Minecraft client")
            return False
        launcher = await find_launcher_window()
        if not launcher:
            command = os.environ.get("MINECRAFT_CLIENT_RESTART_COMMAND", "").strip() or launcher_command()
            await dismiss_crash_popup()
            logger.warning(f"WATCHDOG full graphical client restart started: {command}")
            await asyncio.create_subprocess_shell(command)
            launcher = await _wait_for_launcher()
        if not launcher:
            logger.warning("WATCHDOG launcher timeout")
            return False
        if not await _click_launcher_play(launcher):
            return False
        logger.warning("WATCHDOG PLAY_CLICKED")
        game = await _wait_for_game_window()
        if not game:
            logger.warning("WATCHDOG game-window timeout")
            return False
        await asyncio.sleep(_env_float("MINECRAFT_MAIN_MENU_READY_SECONDS", 8.0, 0.0, 120.0))
        logger.warning("WATCHDOG GAME_WINDOW_READY")
        if not await open_multiplayer_screen(game):
            return False
        return await join_saved_server(game)


async def dismiss_crash_popup() -> bool:
    patterns = (r"Crash report shared", r"game crashed", r"Game crashed")
    for pattern in patterns:
        for window_id in await _search_visible(pattern):
            if await _activate_window(window_id, "crash popup", attempts=3):
                await _run_tool(["xdotool", "key", "Escape"], timeout=2.0)
                logger.warning("WATCHDOG crash popup dismissed")
                return True
    return False


async def ensure_minecraft_client_connected() -> bool:
    """Idempotent: connected means no launch and no duplicate client."""
    if await verify_multiplayer_connected():
        return True
    window_id = await find_minecraft_game_window()
    if window_id:
        if await reconnect_minecraft_client():
            return True
        await open_multiplayer_screen(window_id)
        return await join_saved_server(window_id)
    return await launch_minecraft_client()


async def restart_minecraft_camera_client() -> bool:
    window_id = await find_minecraft_game_window()
    if window_id:
        try:
            await _run_tool(["xdotool", "windowclose", window_id], timeout=3.0)
            await asyncio.sleep(_env_float("MINECRAFT_AFTER_CLOSE_SECONDS", 5.0, 0.0, 60.0))
        except Exception as exc:
            logger.debug(f"WATCHDOG could not close Minecraft game window: {exc}")
    return await launch_minecraft_client()


async def minecraft_window_exists() -> bool:
    return await find_minecraft_game_window() is not None


async def focus_and_click_minecraft() -> bool:
    window_id = await focus_minecraft()
    if not window_id:
        logger.warning("WATCHDOG Minecraft window not found")
        return False
    await _run_tool(["xdotool", "mousemove", "--window", window_id, "80", "80", "click", "1"], timeout=3.0)
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
    return await restart_minecraft_camera_client()


def watchdog_enabled() -> bool:
    return os.environ.get("MINECRAFT_WATCHDOG_ENABLED", "1").strip().lower() not in ("0", "false", "off", "no")
