"""What the browser check tells the one repair pass.

Regressions from DEV on Oct 2:
* a "website" with four stacked cards was cut off top and bottom on a
  one-screen page and still passed;
* "Unexpected token 'var'" was reported with no line, and the repair could not
  find it, twice.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from open_llm_vtuber.room.stage_design import (  # noqa: E402
    preview_line_to_source,
    stage_preview_html,
)
from tests.test_capabilities import _chromium  # noqa: E402

TALL = """<!doctype html><html><head><style>
html,body{margin:0;height:100%;overflow:hidden;background:#0f1d3a;color:#f4ead8;font:40px sans-serif}
.card{height:300px;margin:20px;background:#2a4a86}
</style></head><body>
<h1 id="t">Selwyn Builds</h1>
<div class="card"></div><div class="card"></div><div class="card"></div><div class="card"></div>
<script>setInterval(()=>{document.getElementById('t').style.color='#'+Math.floor(Math.random()*16777215).toString(16)},100)</script>
</body></html>"""

SYNTAX = """<!doctype html>
<html>
<head>
<style>body{margin:0;background:#0f1d3a}</style>
</head>
<body>
<canvas id="c" width="300" height="300"></canvas>
<script>
let speed = 2
let size = 3 var broken = 4;
</script>
</body>
</html>"""


def test_preview_lines_map_back_to_the_source():
    preview = stage_preview_html(SYNTAX).split("\n")
    at = next(i for i, line in enumerate(preview, 1) if "var broken" in line)
    assert preview_line_to_source(SYNTAX, at) == 10
    assert preview_line_to_source(SYNTAX, 2) == 2
    assert preview_line_to_source(SYNTAX, 0) == 0


@pytest.fixture(scope="module")
def qa_base(tmp_path_factory):
    chromium = _chromium()
    if chromium is None:
        pytest.skip("Chromium for Playwright is not installed")
    import os

    import uvicorn

    from tests.harness.fake_tts import FakeTTS
    from tests.harness.full_dev_server import create_app

    os.environ["VR_QA_CHROMIUM"] = chromium
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(ROOT, tts=FakeTTS()),
            host="127.0.0.1",
            port=port,
            log_level="error",
        )
    )
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


def check(base: str, source: str, **kwargs):
    from open_llm_vtuber.room.browser_qa import BrowserQA

    async def run():
        qa = BrowserQA(base, timeout_seconds=30)
        try:
            return await qa.check(source, **kwargs)
        finally:
            await qa.close()

    return asyncio.run(run())


def test_cut_off_content_is_a_problem_unless_the_page_scrolls_itself(qa_base):
    one_screen = check(qa_base, TALL)
    assert not one_screen.ok and one_screen.overflow
    assert any("cut off" in p for p in one_screen.problems())
    website = check(qa_base, TALL, allow_scroll=True)
    assert website.ok and not website.overflow


def test_errors_carry_the_line_and_its_code(qa_base):
    report = check(qa_base, SYNTAX)
    assert not report.ok
    assert any("line 10" in e and "var broken" in e for e in report.errors), (
        report.errors
    )
