import json
import subprocess

from src.open_llm_vtuber.publishing import shorts


def test_make_short_builds_vertical_ffmpeg_command(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    bg = tmp_path / "Clipbg.png"
    source.write_bytes(b"video")
    bg.write_bytes(b"png")
    commands = []

    def fake_run(command, stdout=None, stderr=None, text=None):
        commands.append(command)
        output = tmp_path / "shorts" / command[-1].split("/")[-1]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"mp4")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(shorts, "require_binary", lambda name: name)
    monkeypatch.setattr(shorts.subprocess, "run", fake_run)

    output, sidecar = shorts.make_short(
        source,
        "race",
        request="Mika race Luna",
        viewer="viewer",
        started_at=1000.0,
        finished_at=1010.0,
        output_dir=tmp_path / "shorts",
        background=bg,
    )

    command = commands[0]
    assert output.exists()
    assert sidecar.exists()
    assert "scale=1080:1920" in command[command.index("-filter_complex") + 1]
    assert "scale=1000:563:force_original_aspect_ratio=decrease" in command[command.index("-filter_complex") + 1]
    assert "1:a?" in command
    data = json.loads(sidecar.read_text())
    assert data["event"] == "race"
    assert data["short_file"] == str(output)
