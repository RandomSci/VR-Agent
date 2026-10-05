import asyncio
from types import SimpleNamespace as NS

import numpy as np

from src.open_llm_vtuber.room.minecraft_visual_watchdog import (
    RecoveryStateMachine,
    RecoveryStep,
    VisualFreezeDetector,
    _crop,
    diff_score,
)
from src.open_llm_vtuber.room import minecraft_visual_watchdog as wd


def test_default_crop_is_center_quarter():
    frame = np.zeros((180, 320), dtype=np.uint8)
    cropped = _crop(frame, wd.parse_crop(""))

    assert cropped.shape == (90, 160)


def test_frame_diff_identical_and_noise_are_frozen():
    detector = VisualFreezeDetector(threshold=2.0, strikes_required=3)
    frame = np.zeros((12, 12), dtype=np.uint8)

    assert detector.update(frame).healthy
    assert detector.update(frame.copy()).strikes == 1
    tiny_noise = frame.copy()
    tiny_noise[0, 0] = 1
    sample = detector.update(tiny_noise)

    assert sample.strikes == 2
    assert diff_score(frame, tiny_noise) < 2.0


def test_meaningful_movement_resets_strikes():
    detector = VisualFreezeDetector(threshold=2.0, strikes_required=2)
    frame = np.zeros((10, 10), dtype=np.uint8)
    moved = np.full((10, 10), 40, dtype=np.uint8)

    detector.update(frame)
    assert detector.update(frame).strikes == 1
    sample = detector.update(moved)

    assert sample.strikes == 0
    assert sample.healthy


def test_multiple_low_change_strikes_become_unhealthy():
    detector = VisualFreezeDetector(threshold=2.0, strikes_required=2)
    frame = np.zeros((10, 10), dtype=np.uint8)

    detector.update(frame)
    assert not detector.update(frame).unhealthy
    assert detector.update(frame).unhealthy


def test_recovery_state_machine_focus_reload_backup_and_return():
    machine = RecoveryStateMachine(backup_after_failures=2)

    assert machine.visual_failed() == RecoveryStep.FOCUS_CLICK
    assert machine.focus_result(False) == RecoveryStep.DISCONNECT_RECONNECT
    assert machine.reconnect_result(False) == RecoveryStep.BACKUP
    assert machine.in_backup
    assert machine.recovered() == RecoveryStep.HEALTHY
    assert machine.failures == 0


def test_recovery_state_machine_successful_reconnect_avoids_backup():
    machine = RecoveryStateMachine(backup_after_failures=1)

    machine.visual_failed()
    machine.focus_result(False)

    assert machine.reconnect_result(True) == RecoveryStep.HEALTHY
    assert not machine.in_backup


def test_recovery_cannot_run_concurrently_and_cooldown_blocks_restart():
    machine = RecoveryStateMachine(cooldown_seconds=30)

    assert machine.start_recovery(100.0)
    assert not machine.start_recovery(101.0)
    machine.finish_recovery(False, 110.0)

    assert not machine.can_start(120.0)
    assert machine.can_start(140.0)


def test_broad_minecraft_title_detection_excludes_launcher(monkeypatch):
    monkeypatch.setattr(wd.shutil, "which", lambda name: "/usr/bin/xdotool")

    async def run_tool(args, timeout=4.0):
        if args[:4] == ["xdotool", "search", "--onlyvisible", "--name"]:
            assert args[4] == r"Minecraft 1\.21\.6"
            return NS(returncode=0, stdout="10\n11\n", stderr="")
        if args[:2] == ["xdotool", "getwindowname"]:
            return NS(
                returncode=0,
                stdout="Minecraft Launcher\n" if args[2] == "10" else "Minecraft 1.21.6 - Multiplayer (3rd-party Server)\n",
                stderr="",
            )
        return NS(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(wd, "_run_tool", run_tool)

    assert asyncio.run(wd.find_minecraft_game_window()) == "11"
    assert asyncio.run(wd.verify_multiplayer_connected()) is True


def test_ensure_connected_does_not_launch_duplicate(monkeypatch):
    launched = False

    async def launch():
        nonlocal launched
        launched = True
        return False

    monkeypatch.setattr(wd, "verify_multiplayer_connected", lambda: _async_true())
    monkeypatch.setattr(wd, "launch_minecraft_client", launch)

    assert asyncio.run(wd.ensure_minecraft_client_connected()) is True
    assert launched is False


def test_launcher_timeout(monkeypatch):
    async def no_launcher():
        return None

    async def fake_shell(command):
        return NS(returncode=None)

    monkeypatch.setattr(wd.shutil, "which", lambda name: "/usr/bin/xdotool")
    monkeypatch.setattr(wd, "verify_multiplayer_connected", lambda: _async_false())
    monkeypatch.setattr(wd, "find_launcher_window", no_launcher)
    monkeypatch.setattr(wd, "_wait_for_launcher", lambda timeout=None: _async_none())
    monkeypatch.setattr(wd.asyncio, "create_subprocess_shell", fake_shell)

    assert asyncio.run(wd.launch_minecraft_client()) is False


def test_game_window_timeout_after_launcher_play(monkeypatch):
    monkeypatch.setattr(wd.shutil, "which", lambda name: "/usr/bin/xdotool")
    monkeypatch.setattr(wd, "verify_multiplayer_connected", lambda: _async_false())
    monkeypatch.setattr(wd, "find_launcher_window", lambda: _async_value("1"))
    monkeypatch.setattr(wd, "_click_launcher_play", lambda window_id: _async_true())
    monkeypatch.setattr(wd, "_wait_for_game_window", lambda timeout=None: _async_none())

    assert asyncio.run(wd.launch_minecraft_client()) is False


def test_reconnect_timeout(monkeypatch):
    monkeypatch.setattr(wd, "verify_multiplayer_connected", lambda: _async_false())
    monkeypatch.setattr(wd, "find_minecraft_game_window", lambda: _async_value("2"))
    monkeypatch.setattr(wd, "join_saved_server", lambda window_id: _async_false())

    assert asyncio.run(wd.reconnect_minecraft_client()) is False


def test_xdotool_activation_timeout_retries(monkeypatch):
    attempts = []

    async def run_tool(args, timeout=4.0):
        if args[:3] == ["xdotool", "windowactivate", "--sync"]:
            attempts.append(args[-1])
            return NS(returncode=1 if len(attempts) < 3 else 0, stdout="", stderr="timeout")
        return NS(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(wd, "_run_tool", run_tool)

    assert asyncio.run(wd._activate_window("7", attempts=4)) is True
    assert len(attempts) == 3


def test_multiplayer_return_ignored_then_succeeds(monkeypatch):
    confirms = [False, True]
    keys = []

    async def run_tool(args, timeout=4.0):
        if args[:2] == ["xdotool", "key"]:
            keys.append(args[2])
        return NS(returncode=0, stdout="", stderr="")

    async def confirm(window_id):
        return confirms.pop(0)

    monkeypatch.setattr(wd, "find_minecraft_game_window", lambda: _async_value("3"))
    monkeypatch.setattr(wd, "_activate_window", lambda window_id, label="game", attempts=10: _async_true())
    monkeypatch.setattr(wd, "_run_tool", run_tool)
    monkeypatch.setattr(wd, "confirm_multiplayer_menu", confirm)

    assert asyncio.run(wd.open_multiplayer_screen("3")) is True
    assert keys.count("Return") == 2


def test_keyboard_multiplayer_fails_mouse_fallback_succeeds(monkeypatch):
    confirms = [False, False, False, False, False, True]
    clicks = []

    async def run_tool(args, timeout=4.0):
        if args[:2] == ["xdotool", "click"]:
            clicks.append(args)
        return NS(returncode=0, stdout="", stderr="")

    async def confirm(window_id):
        return confirms.pop(0)

    monkeypatch.setattr(wd, "find_minecraft_game_window", lambda: _async_value("4"))
    monkeypatch.setattr(wd, "_activate_window", lambda window_id, label="game", attempts=10: _async_true())
    monkeypatch.setattr(wd, "_run_tool", run_tool)
    monkeypatch.setattr(wd, "confirm_multiplayer_menu", confirm)

    assert asyncio.run(wd.open_multiplayer_screen("4")) is True
    assert clicks


def test_saved_server_only_clicked_after_multiplayer_confirmed(monkeypatch):
    clicks = []

    async def run_tool(args, timeout=4.0):
        if args[:2] == ["xdotool", "click"]:
            clicks.append(args)
        return NS(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(wd, "confirm_multiplayer_menu", lambda window_id: _async_false())
    monkeypatch.setattr(wd, "_run_tool", run_tool)

    assert asyncio.run(wd.join_saved_server("5")) is False
    assert clicks == []


def test_crash_popup_handling(monkeypatch):
    keys = []

    async def search(pattern):
        return ["9"] if "Crash" in pattern else []

    async def run_tool(args, timeout=4.0):
        if args[:2] == ["xdotool", "key"]:
            keys.append(args[2])
        return NS(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(wd, "_search_visible", search)
    monkeypatch.setattr(wd, "_activate_window", lambda window_id, label="game", attempts=10: _async_true())
    monkeypatch.setattr(wd, "_run_tool", run_tool)

    assert asyncio.run(wd.dismiss_crash_popup()) is True
    assert "Escape" in keys


async def _async_true():
    return True


async def _async_false():
    return False


async def _async_none():
    return None


async def _async_value(value):
    return value
