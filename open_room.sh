#!/usr/bin/env bash
# Opens the VR Agent room in its own browser window with sound allowed
# automatically (no "click to enable audio"). Start the server first.
URL="${1:-http://127.0.0.1:12393/vr-agent/room.html}"
PROFILE="${HOME}/.vr-agent-room-browser"
FLAGS=(--autoplay-policy=no-user-gesture-required "--user-data-dir=${PROFILE}" --no-first-run --window-size=1920,1080 "--app=${URL}")
for app in \
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge" \
  google-chrome google-chrome-stable chromium chromium-browser microsoft-edge; do
  if [ -x "$app" ] || command -v "$app" >/dev/null 2>&1; then
    "$app" "${FLAGS[@]}" >/dev/null 2>&1 &
    exit 0
  fi
done
echo "Could not find Chrome or Edge. Open ${URL} in OBS as a Browser Source instead."
exit 1
