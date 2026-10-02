"""numpy must not try to start a math thread per CPU core inside the sandbox.

Regression from Selwyn's desktop: "OpenBLAS blas_thread_init: pthread_create
failed" killed matplotlib animations, because the sandbox's memory and
process limits do not leave room for one OpenBLAS thread per core.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from open_llm_vtuber.room import coding_lesson as cl  # noqa: E402


def test_math_libraries_use_one_thread():
    env = cl.sandbox_environment()
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        assert env[name] == "1"


def test_the_process_limit_leaves_room_above_what_the_user_already_runs():
    assert cl.sandbox_process_limit() >= 128
