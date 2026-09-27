#!/usr/bin/env bash
# Stops the VR Agent server started by the "VR Agent" app icon.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PID_FILE="$ROOT/logs/vr_agent_server.pid"
stopped=0
if [ -f "$PID_FILE" ]; then
  pid="$(cat "$PID_FILE")"
  if kill -- "-$pid" 2>/dev/null || kill "$pid" 2>/dev/null; then stopped=1; fi
  rm -f "$PID_FILE"
fi
command -v notify-send >/dev/null 2>&1 && {
  [ "$stopped" = 1 ] && notify-send "VR Agent" "Server stopped." || notify-send "VR Agent" "Server was not running."
}
