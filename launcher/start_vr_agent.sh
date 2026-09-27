#!/usr/bin/env bash
# Starts the VR Agent server in the background (if it is not running yet),
# waits until it answers, then opens OBS Studio (Flatpak). No terminal needed:
# the "VR Agent" app icon runs this. Server output goes to logs/vr_agent_server.log.
set -u
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${VR_AGENT_PORT:-12393}"
LOG_DIR="$ROOT/logs"
LOG="$LOG_DIR/vr_agent_server.log"
mkdir -p "$LOG_DIR"

notify() { command -v notify-send >/dev/null 2>&1 && notify-send "VR Agent" "$1"; return 0; }
server_up() { curl -s -o /dev/null --max-time 2 "http://127.0.0.1:${PORT}/" ; }

find_uv() {
  for c in uv "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" "$HOME/miniconda3/bin/uv" "$HOME/anaconda3/bin/uv" /usr/local/bin/uv; do
    if command -v "$c" >/dev/null 2>&1; then command -v "$c"; return 0; fi
  done
  return 1
}

if server_up; then
  notify "Server already running."
else
  UV="$(find_uv)" || { notify "Could not find uv. Install it or start the server once from a terminal."; exit 1; }
  cd "$ROOT" || exit 1
  echo "=== $(date) starting VR Agent ===" >>"$LOG"
  # Own process group, so "Stop VR Agent" stops uv and the server together.
  setsid nohup "$UV" run run_server.py >>"$LOG" 2>&1 < /dev/null &
  echo $! >"$LOG_DIR/vr_agent_server.pid"
  notify "Starting the server..."
  for _ in $(seq 1 90); do
    server_up && break
    sleep 1
  done
  if server_up; then
    notify "Server is up. Opening OBS."
  else
    notify "Server did not start. See logs/vr_agent_server.log"
    exit 1
  fi
fi

if command -v flatpak >/dev/null 2>&1 && flatpak info com.obsproject.Studio >/dev/null 2>&1; then
  flatpak run com.obsproject.Studio >/dev/null 2>&1 &
elif command -v obs >/dev/null 2>&1; then
  obs >/dev/null 2>&1 &
else
  notify "OBS not found. Open it yourself; the server is running."
fi
