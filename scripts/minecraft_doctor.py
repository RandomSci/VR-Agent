"""What went wrong on a Minecraft stream? Reads the logs and prints a timeline.

    uv run scripts/minecraft_doctor.py                     # today, the whole day
    uv run scripts/minecraft_doctor.py 2026-10-04 10:20 10:40   # a date and a time window

It looks at:
  logs/debug_<date>*.log        everything the server printed (the terminal)
  logs/minecraft-server.log     the Minecraft server (lag, kicks, crashes)
  minecraft/server/logs/        the server's own logs (latest.log)
  logs/mindcraft.log            the bots (errors, disconnects)
  ~/.minecraft/logs, crash-reports   YOUR game (the camera): did it crash or
                                run out of memory? (the launcher's terminal
                                only shows the launcher, not the game)

and prints, in time order: server lag ("Can't keep up"), players joining,
leaving, timing out or being kicked, bots restarting, the camera losing
Mika, the server being overloaded, builds starting and finishing, and how
long each viewer waited for an answer (slow ones are flagged).
"""

from __future__ import annotations

import gzip
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
SERVER_LOGS = ROOT / "minecraft" / "server" / "logs"

SERVER_PATTERNS = [
    (r"Can't keep up", "SERVER LAG"),
    (r"OutOfMemory|java\.lang\.\w+Error|Exception", "SERVER ERROR"),
    (r"lost connection|Timed out|timed out", "DISCONNECT"),
    (r"was kicked|Kicked|kicked for", "KICK"),
    (r"moved too quickly|moved wrongly", "MOVEMENT"),
    (r"joined the game", "JOIN"),
    (r"left the game", "LEAVE"),
    (r"Stopping server|Done \(", "SERVER"),
]
OURS_PATTERNS = [
    (r"left the world, restarting Mindcraft|restarting Mindcraft|starting again in", "BOTS RESTART"),
    (r"stopped \(exit", "PROCESS EXIT"),
    (r"Minecraft camera:|is the camera now|left, back to the web views", "CAMERA"),
    (r"overloaded|keeps up again", "SERVER SPEED"),
    (r"has not moved|still frozen", "FROZEN"),
    (r"pulled \w+ back", "PULLED BACK"),
    (r"no contact with Mindcraft", "BOTS LINK"),
    (r"connected to Mindcraft|Mindcraft link", "BOTS LINK"),
    (r"building '|the Kingdom, lot", "BUILD"),
    (r"free build failed|building failed|could not be designed", "BUILD ERROR"),
    (r"ERROR|Traceback", "ERROR"),
]
CLIENT_DIR = Path.home() / ".minecraft"
CLIENT_PATTERNS = [
    (r"OutOfMemory|Out of memory", "GAME MEMORY"),
    (r"Stopping!|Game crashed|crash report|---- Minecraft Crash Report", "GAME CRASH"),
    (r"Disconnected|Lost connection|Connection reset|Timed out", "GAME DISCONNECT"),
    (r"Can't keep up|took too long|Exception", "GAME ERROR"),
    (r"Setting user:|Connecting to", "GAME START"),
]
RECEIVED = re.compile(r"YouTube message from @?([^:]+): (.*)")
ANSWERED = re.compile(r"Minecraft: \w+ answers ([^(]+) \(")


def _open(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", errors="replace")
    return open(path, errors="replace")


def _in_window(clock: str, start: str, end: str) -> bool:
    return (not start or clock >= start) and (not end or clock <= end)


def main() -> None:
    day = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    start = sys.argv[2] if len(sys.argv) > 2 else ""
    end = sys.argv[3] if len(sys.argv) > 3 else ""
    events: list[tuple[str, str, str]] = []

    # 1) our own log: the terminal output, kept in logs/debug_<date>*.log
    received: dict[str, list[str]] = {}
    waits: list[tuple[str, str, float]] = []
    for path in sorted(LOGS.glob(f"debug_{day}*.log")):
        for line in _open(path):
            m = re.match(r"(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)", line)
            if not m or not _in_window(m.group(2), start, end):
                continue
            clock = m.group(2)
            message = line.split(" | ", 3)[-1].strip()
            if message.endswith("}") and " | " in message:
                message = message.rsplit(" | ", 1)[0]  # the extra data at the end of each line
            got = RECEIVED.search(message)
            if got:
                received.setdefault(got.group(1).strip(), []).append(clock)
            ans = ANSWERED.search(message)
            if ans:
                for who in [w.strip() for w in ans.group(1).split(",")]:
                    times = received.get(who) or received.get("@" + who) or []
                    if times:
                        asked = times.pop(0)
                        t1 = datetime.strptime(asked, "%H:%M:%S")
                        t2 = datetime.strptime(clock, "%H:%M:%S")
                        waits.append((clock, who, (t2 - t1).total_seconds()))
            for pattern, label in OURS_PATTERNS:
                if re.search(pattern, message):
                    events.append((clock, label, message[:180]))
                    break

    # 2) the Minecraft server
    server_files = [LOGS / "minecraft-server.log"] + sorted(SERVER_LOGS.glob(f"{day}-*.log.gz")) + [SERVER_LOGS / "latest.log"]
    for path in server_files:
        if not path.exists():
            continue
        for line in _open(path):
            m = re.match(r"\[(\d\d:\d\d:\d\d)\]", line)
            if not m or not _in_window(m.group(1), start, end):
                continue
            for pattern, label in SERVER_PATTERNS:
                if re.search(pattern, line):
                    events.append((m.group(1), label, line.strip()[:180]))
                    break

    # 3) your own game (the camera): its log of today, and crash reports
    client_logs = sorted((CLIENT_DIR / "logs").glob(f"{day}-*.log.gz")) + [CLIENT_DIR / "logs" / "latest.log"]
    for path in client_logs:
        if not path.exists():
            continue
        for line in _open(path):
            m = re.match(r"\[(\d\d:\d\d:\d\d)\]", line)
            if not m or not _in_window(m.group(1), start, end):
                continue
            for pattern, label in CLIENT_PATTERNS:
                if re.search(pattern, line):
                    events.append((m.group(1), label, line.strip()[:180]))
                    break
    for path in sorted((CLIENT_DIR / "crash-reports").glob(f"crash-{day}_*.txt")):
        clock = path.name[len(f"crash-{day}_"):][:8].replace(".", ":")
        reason = next((ln.strip() for ln in _open(path) if ln.startswith("Description:")), "")
        events.append((clock, "GAME CRASH", f"{path.name} {reason}"))

    # 4) the bots (Mindcraft has no times in its log: the last errors)
    mind = LOGS / "mindcraft.log"
    mind_errors = []
    if mind.exists():
        for line in _open(mind):
            if re.search(r"error|Error|disconnect|kicked|ECONNRESET|timed out|crash", line):
                mind_errors.append(line.strip()[:180])

    events.sort()
    seen = set()
    print(f"=== Timeline {day} {start or '00:00'}-{end or '23:59'} ===")
    for clock, label, text in events:
        key = (clock, label, text)
        if key in seen:
            continue
        seen.add(key)
        print(f"{clock}  {label:<13} {text}")
    lag = [e for e in events if e[1] == "SERVER LAG"]
    print()
    print(f"Server lag warnings: {len(lag)}" + (f" (first {lag[0][0]}, last {lag[-1][0]})" if lag else ""))
    slow = [w for w in waits if w[2] > 60]
    if waits:
        avg = sum(w[2] for w in waits) / len(waits)
        print(f"Viewer answers: {len(waits)}, average wait {avg:.0f} s, waited over a minute: {len(slow)}")
        for clock, who, secs in slow[:30]:
            print(f"  {clock}  {who} waited {secs / 60:.1f} min")
    if mind_errors:
        print(f"\nLast bot (Mindcraft) errors ({len(mind_errors)} in all):")
        for line in mind_errors[-25:]:
            print("  " + line)
    if not events and not waits:
        print("Nothing found: are the logs in logs/? (run this from the project folder)")


if __name__ == "__main__":
    main()
