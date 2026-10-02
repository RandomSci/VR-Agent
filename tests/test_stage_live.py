"""Code in Public on a live stream: runs, previews and sound.

* Python runs: the sandbox helper must live in a folder the sandbox mounts.
  Regression for "bwrap: execvp ~/.local/share/uv/python/...: No such file".
* The code-writing prompt knows it is live (nobody can click) and carries the
  stream's design kit.
* The web preview gets the autopilot; the source on screen does not.
* The Stage page is served with the sound guard, and the server records
  whether the browser played or blocked the audio.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from open_llm_vtuber.room.coding_actions import generate_system  # noqa: E402
from open_llm_vtuber.room.coding_lesson import (  # noqa: E402
    CodingLesson,
    RestrictedScriptRunner,
)
from open_llm_vtuber.room.stage_design import (  # noqa: E402
    AUTOPILOT_JS,
    SPEECH_LIVE_NOTE,
    stage_preview_html,
)

# A stand-in for bubblewrap that fails exactly like the real one when the
# program it must start is not inside a mounted folder, and otherwise runs it.
FAKE_BWRAP = textwrap.dedent(
    """\
    #!{python}
    import os, subprocess, sys
    args = sys.argv[1:]
    mounts, lesson_root = [], None
    i = 0
    while i < len(args) and args[i] != "--":
        if args[i] in ("--ro-bind", "--bind"):
            src, dst = args[i + 1], args[i + 2]
            mounts.append(src)
            if dst == "/lesson":
                lesson_root = src
            i += 3
            continue
        i += 1
    command = args[i + 1:]
    helper = os.path.realpath(command[0])
    if not any(helper == m or helper.startswith(m.rstrip("/") + "/") for m in mounts):
        print(f"bwrap: execvp {{command[0]}}: No such file or directory", file=sys.stderr)
        sys.exit(1)
    command = [c.replace("/lesson/", lesson_root + "/") for c in command]
    sys.exit(subprocess.call(command, cwd=lesson_root))
    """
)


@pytest.fixture
def fake_bwrap(tmp_path, monkeypatch):
    tool = tmp_path / "bin" / "bwrap"
    tool.parent.mkdir()
    tool.write_text(FAKE_BWRAP.format(python=sys.executable))
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tool.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    return tool


def system_python():
    for candidate in ("/usr/bin/python3", "/bin/python3"):
        if Path(candidate).is_file():
            return candidate
    return None


def test_python_runs_when_the_server_python_is_not_mounted(fake_bwrap, monkeypatch):
    python = system_python()
    if not python:
        pytest.skip("no system python3")
    # The coding interpreter, like For_AI on Selwyn's machine.
    monkeypatch.setenv("VR_CODING_PYTHON", python)
    monkeypatch.setenv("VR_CODING_PYTHON_PREFIX", "/usr")
    lesson = CodingLesson(student_id="dev:selwyn", student_name="@selwyn", teacher="mika", goal="hi")
    lesson.set_artifact("dev:selwyn", "python", "print('hello from the live stream')\n")
    result = asyncio.run(RestrictedScriptRunner().run(lesson, "dev:selwyn"))
    assert "execvp" not in (result.stderr or ""), result.stderr
    assert result.exit_code == 0, result
    assert "hello from the live stream" in result.stdout


def test_the_code_prompt_knows_it_is_live():
    web = generate_system("web", "a single self-contained HTML document")
    python = generate_system("python", "a single headless Python file")
    for prompt in (web, python):
        assert "LIVE ON A YOUTUBE STREAM" in prompt
        assert "click" in prompt.lower()
    # The web prompt carries the stream's design kit and a starting shape.
    assert "#ffb55e" in web and "--lamp" in web
    assert "requestAnimationFrame" in web
    # Python styling comes with the kind: a chart gets the stream look, a
    # lesson gets no chart instructions at all.
    chart = generate_system("python", "a single headless Python file", "python_chart")
    lesson = generate_system("python", "a single headless Python file", "python_lesson")
    assert "dark_background" in chart
    assert "matplotlib" not in lesson.split("WHAT TO BUILD")[0]
    assert "No charts, no matplotlib" in lesson
    assert "click" in SPEECH_LIVE_NOTE


def test_the_preview_gets_the_autopilot_but_the_source_does_not():
    source = "<!doctype html><html><head><title>x</title></head><body><button>Start</button></body></html>"
    preview = stage_preview_html(source)
    assert AUTOPILOT_JS.strip()[:40] in preview
    # First thing in <head>, so dialogs are disabled before the program runs.
    assert preview.index("__vrStageAutopilot") < preview.index("<title>")
    assert "__vrStageAutopilot" not in source
    # Pages without a <head> still get it.
    assert "__vrStageAutopilot" in stage_preview_html("<html><body>hi</body></html>")
    assert "__vrStageAutopilot" in stage_preview_html("<canvas></canvas>")
    assert stage_preview_html("") == ""


def test_the_stage_page_is_served_with_the_sound_guard(tmp_path):
    from fastapi.testclient import TestClient

    from tests.harness.fake_tts import FakeTTS
    from tests.harness.full_dev_server import create_app

    # A minimal DEV root: the real room, and a stand-in Stage page.
    (tmp_path / "frontend" / "vr-agent").mkdir(parents=True)
    (tmp_path / "frontend" / "vr-agent" / "teaching-stage.html").write_text(
        "<!doctype html><html><head><title>stage</title></head><body></body></html>"
    )
    (tmp_path / "room").symlink_to(ROOT / "room", target_is_directory=True)
    app = create_app(tmp_path, tts=FakeTTS())
    client = TestClient(app)

    page = client.get("/vr-agent/teaching-stage.html").text
    assert '<script src="/harness/sound-guard.js"></script>' in page
    assert page.index("sound-guard.js") < page.index("</head>")
    guard = client.get("/harness/sound-guard.js")
    assert guard.status_code == 200 and "NotAllowedError" in guard.text

    client.post("/harness/sound", json={"state": "blocked", "detail": "NotAllowedError"})
    assert app.state.sound["state"] == "blocked"
    client.post("/harness/sound", json={"state": "playing"})
    assert app.state.sound["state"] == "playing"


def test_a_python_animation_comes_out_as_a_gif(fake_bwrap, monkeypatch):
    """Python can move: FuncAnimation saves output.gif and the Stage gets it."""
    import subprocess
    import sys as _sys

    # The coding Python (For_AI on stream; start-teaching.sh exports it).
    coding_python = os.environ.get("VR_CODING_PYTHON") or _sys.executable
    prefix = os.environ.get("VR_CODING_PYTHON_PREFIX") or _sys.prefix
    check = subprocess.run([coding_python, "-c", "import matplotlib, PIL"], capture_output=True)
    if check.returncode != 0:
        pytest.skip("the coding Python has no matplotlib and Pillow")
    monkeypatch.setenv("VR_CODING_PYTHON", coding_python)
    monkeypatch.setenv("VR_CODING_PYTHON_PREFIX", prefix)
    code = (
        "import numpy as np\nimport matplotlib\nmatplotlib.use('Agg')\n"
        "import matplotlib.pyplot as plt\n"
        "from matplotlib.animation import FuncAnimation, PillowWriter\n"
        "fig, ax = plt.subplots(figsize=(6.4, 3.6), dpi=50)\n"
        "dots = ax.scatter(np.random.rand(40), np.random.rand(40))\n"
        "def update(i):\n    dots.set_sizes(10 + 40 * np.random.rand(40))\n    return (dots,)\n"
        "FuncAnimation(fig, update, frames=12, blit=True).save('output.gif', writer=PillowWriter(fps=12))\n"
        "print('saved 12 frames')\n"
    )
    lesson = CodingLesson(student_id="dev:selwyn", student_name="@selwyn", teacher="mika", goal="twinkle")
    lesson.set_artifact("dev:selwyn", "python", code)
    runner = RestrictedScriptRunner()
    result = asyncio.run(runner.run(lesson, "dev:selwyn"))
    assert result.exit_code == 0, result.stderr or result.error
    gifs = [a for a in runner.artifacts(lesson) if a["mime"] == "image/gif"]
    assert gifs and gifs[0]["name"] == "output.gif"


def test_generated_code_never_sees_server_secrets(fake_bwrap, monkeypatch):
    """A viewer asking for os.environ must not put a token on stream."""
    monkeypatch.setenv("GITHUB_PUBLISH_TOKEN", "ghp_should_never_leak")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-leak")
    python = system_python()
    if not python:
        pytest.skip("no system python3")
    monkeypatch.setenv("VR_CODING_PYTHON", python)
    monkeypatch.setenv("VR_CODING_PYTHON_PREFIX", "/usr")
    lesson = CodingLesson(student_id="dev:selwyn", student_name="@selwyn", teacher="mika", goal="env")
    lesson.set_artifact("dev:selwyn", "python", "import os\nprint(dict(os.environ))\n")
    result = asyncio.run(RestrictedScriptRunner().run(lesson, "dev:selwyn"))
    assert result.exit_code == 0, result.stderr
    assert "should_never_leak" not in result.stdout
    assert "should-never-leak" not in result.stdout
    assert "PATH" in result.stdout  # it ran and printed its (tiny) environment
