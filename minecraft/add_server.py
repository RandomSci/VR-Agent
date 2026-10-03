"""Adds "Mika and Luna (local)" to your Minecraft multiplayer list, and lets
your game keep running when it is not the focused window.

Joining that server with your own game makes it the stream camera (see
VR_MINECRAFT_PLAYER in room/minecraft_mode.py). Your existing servers are
kept and the old list is backed up as servers.dat.bak. In options.txt only
pauseOnLostFocus is changed (a backup is kept as options.txt.bak): without
it, the pause menu covers the stream whenever you click OBS.
"""

from __future__ import annotations

import io
import os
import shutil
import struct
import sys
from pathlib import Path

NAME = "Mika and Luna (local)"


def _read(stream: io.BytesIO, tag: int):
    if tag == 1:
        return struct.unpack(">b", stream.read(1))[0]
    if tag == 2:
        return struct.unpack(">h", stream.read(2))[0]
    if tag == 3:
        return struct.unpack(">i", stream.read(4))[0]
    if tag == 4:
        return struct.unpack(">q", stream.read(8))[0]
    if tag == 5:
        return struct.unpack(">f", stream.read(4))[0]
    if tag == 6:
        return struct.unpack(">d", stream.read(8))[0]
    if tag == 7:
        n = struct.unpack(">i", stream.read(4))[0]
        return bytearray(stream.read(n))
    if tag == 8:
        n = struct.unpack(">H", stream.read(2))[0]
        return stream.read(n).decode("utf-8", "replace")
    if tag == 9:
        inner = stream.read(1)[0]
        n = struct.unpack(">i", stream.read(4))[0]
        return ("list", inner, [_read(stream, inner) for _ in range(n)])
    if tag == 10:
        out = []
        while True:
            t = stream.read(1)[0]
            if t == 0:
                return ("compound", out)
            name = _read(stream, 8)
            out.append((t, name, _read(stream, t)))
    if tag == 11:
        n = struct.unpack(">i", stream.read(4))[0]
        return list(struct.unpack(f">{n}i", stream.read(4 * n)))
    if tag == 12:
        n = struct.unpack(">i", stream.read(4))[0]
        return list(struct.unpack(f">{n}q", stream.read(8 * n)))
    raise ValueError(f"unknown tag {tag}")


def _write(out: io.BytesIO, tag: int, value) -> None:
    if tag == 1:
        out.write(struct.pack(">b", value))
    elif tag == 2:
        out.write(struct.pack(">h", value))
    elif tag == 3:
        out.write(struct.pack(">i", value))
    elif tag == 4:
        out.write(struct.pack(">q", value))
    elif tag == 5:
        out.write(struct.pack(">f", value))
    elif tag == 6:
        out.write(struct.pack(">d", value))
    elif tag == 7:
        out.write(struct.pack(">i", len(value)) + bytes(value))
    elif tag == 8:
        data = value.encode("utf-8")
        out.write(struct.pack(">H", len(data)) + data)
    elif tag == 9:
        _kind, inner, items = value
        out.write(bytes([inner]) + struct.pack(">i", len(items)))
        for item in items:
            _write(out, inner, item)
    elif tag == 10:
        for t, name, item in value[1]:
            out.write(bytes([t]))
            _write(out, 8, name)
            _write(out, t, item)
        out.write(b"\x00")
    elif tag == 11:
        out.write(struct.pack(">i", len(value)) + struct.pack(f">{len(value)}i", *value))
    elif tag == 12:
        out.write(struct.pack(">i", len(value)) + struct.pack(f">{len(value)}q", *value))


# Stream camera settings in the game's options.txt: never pause when OBS has
# the focus, and never drop to 10 frames a second just because nobody touches
# the keyboard (the camera is never touched; "afk" made the stream choppy).
STREAM_OPTIONS = {"pauseOnLostFocus": "false", "inactivityFpsLimit": '"minimized"'}


def keep_running_unfocused(game_dir: Path) -> None:
    path = game_dir / "options.txt"
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    wanted = [f"{k}:{v}" for k, v in STREAM_OPTIONS.items()]
    if all(w in lines for w in wanted):
        return
    out = [line for line in lines if line.split(":", 1)[0] not in STREAM_OPTIONS] + wanted
    shutil.copy2(path, path.with_suffix(".txt.bak"))
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    print("Minecraft: keeps running and full speed when OBS has the focus (restart the game to apply).")


def main() -> int:
    game_dir = Path.home() / ".minecraft"
    if game_dir.exists():
        keep_running_unfocused(game_dir)
    return add_server()


def add_server() -> int:
    port = os.environ.get("VR_MINECRAFT_PORT", "25565")
    address = "localhost" if port == "25565" else f"localhost:{port}"
    game_dir = Path.home() / ".minecraft"
    if not game_dir.exists():
        print("Minecraft Java has not been started on this PC yet, skipping the server list.")
        return 0
    path = game_dir / "servers.dat"
    entry = ("compound", [(8, "name", NAME), (8, "ip", address), (1, "hidden", 0)])
    root = ("compound", [(9, "servers", ("list", 10, []))])
    if path.exists() and path.stat().st_size > 0:
        stream = io.BytesIO(path.read_bytes())
        if stream.read(1)[0] != 10:
            print("servers.dat looks unusual, leaving it alone.")
            return 0
        _read(stream, 8)
        root = _read(stream, 10)
    servers = next((v for t, n, v in root[1] if n == "servers" and t == 9), None)
    if servers is None:
        servers = ("list", 10, [])
        root[1].append((9, "servers", servers))
    if servers[1] not in (10, 0):
        print("servers.dat looks unusual, leaving it alone.")
        return 0
    for item in servers[2]:
        fields = {n: v for _t, n, v in item[1]}
        if fields.get("ip") == address:
            print(f"Your server list already has {address}.")
            return 0
    if servers[1] == 0:
        servers = ("list", 10, [])
        root[1][:] = [x for x in root[1] if x[1] != "servers"] + [(9, "servers", servers)]
    servers[2].append(entry)
    out = io.BytesIO()
    out.write(b"\x0a")
    _write(out, 8, "")
    _write(out, 10, root)
    if path.exists():
        shutil.copy2(path, path.with_suffix(".dat.bak"))
    path.write_bytes(out.getvalue())
    print(f'Added "{NAME}" ({address}) to your Minecraft multiplayer list.')
    return 0


if __name__ == "__main__":
    sys.exit(main())
