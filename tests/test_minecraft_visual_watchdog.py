import numpy as np

from src.open_llm_vtuber.room.minecraft_visual_watchdog import (
    RecoveryStateMachine,
    RecoveryStep,
    VisualFreezeDetector,
    diff_score,
)


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
    assert machine.focus_result(False) == RecoveryStep.CLIENT_RELOAD
    assert machine.reload_result(False) == RecoveryStep.BACKUP
    assert machine.in_backup
    assert machine.recovered() == RecoveryStep.HEALTHY
    assert machine.failures == 0
