"""Class mode's notebook kernel: cells share variables, like Jupyter."""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from open_llm_vtuber.room.notebook_kernel import NotebookKernel  # noqa: E402


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap is not installed")
def test_cells_share_state_and_errors_are_readable(monkeypatch):
    monkeypatch.setenv("VR_CODING_PYTHON", "/usr/bin/python3")
    monkeypatch.setenv("VR_CODING_PYTHON_PREFIX", "/usr")

    async def go():
        kernel = NotebookKernel()
        try:
            first = await kernel.execute("snacks = ['burger']\nsnacks.append('subway')\nsnacks")
            second = await kernel.execute("len(snacks)")
            broken = await kernel.execute("x = 1\n1/0")
            stuck = await kernel.execute("while True: pass", timeout=3)
            after = await kernel.execute("'still here'")
            return first, second, broken, stuck, after
        finally:
            await kernel.close()

    first, second, broken, stuck, after = asyncio.run(go())
    assert first["result"]["text"] == "['burger', 'subway']"
    assert second["result"]["text"] == "2"
    assert broken["error"]["name"] == "ZeroDivisionError" and broken["error"]["line"] == 2
    assert stuck["error"]["name"] == "Timeout" and stuck.get("restarted")
    assert after["result"]["text"] == "'still here'"
