#!/usr/bin/env bash
# Go live from a plain terminal (no VS Code needed):
#   ./go-live.sh            (add --fresh to start the course from lesson 1)
# Starts the server, and if it ever crashes it starts again by itself.
# Press Ctrl+C to stop for real.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

# ./go-live.sh --fresh   starts the course again from lesson 1
if [ "${1:-}" = "--fresh" ]; then
    rm -f data/class/progress.json
    echo "Class progress cleared: starting from lesson 1"
fi

# The notebook and the coding runs use VR_CODING_PYTHON from .env.
CODING_PY="$(grep -E '^VR_CODING_PYTHON=' .env 2>/dev/null | cut -d= -f2- | tr -d '"')"
if [ -n "$CODING_PY" ] && [ -x "$CODING_PY" ]; then
    missing="$("$CODING_PY" - <<'PY'
import importlib.util
need = {"numpy": "numpy", "sympy": "sympy", "matplotlib": "matplotlib", "pandas": "pandas"}
print(" ".join(p for m, p in need.items() if importlib.util.find_spec(m) is None))
PY
)"
    if [ -n "$missing" ]; then
        echo "Notebook Python is missing: $missing"
        echo "  Install once with: $CODING_PY -m pip install $missing"
    fi
fi

echo "Stage for OBS: http://127.0.0.1:12393/vr-agent/teaching-stage.html"

# VR_START_OBS=1 in .env: OBS opens (minimized) once the server is up. The server then picks
# the Stage scene, reloads the page without cache, waits for Mika and Luna,
# and only then starts streaming. Ctrl+C ends the stream and closes OBS.
# setsid: Ctrl+C in this terminal must not kill OBS before the goodbye.
# OBS opens only once the server answers, so its Browser source never loads
# a dead page (that was the black screen).
open_obs_when_server_is_up() {
    for _ in $(seq 1 90); do
        curl -s -o /dev/null "http://127.0.0.1:12393/vr-agent/teaching-stage.html" && break
        sleep 2
    done
    echo "Opening OBS (minimized)"
    if command -v obs >/dev/null; then
        setsid obs --minimize-to-tray --disable-shutdown-check >/dev/null 2>&1 &
    else
        setsid flatpak run com.obsproject.Studio --minimize-to-tray --disable-shutdown-check >/dev/null 2>&1 &
    fi
}
if grep -qE '^VR_START_OBS=(1|true)' .env 2>/dev/null && ! pgrep -x obs >/dev/null; then
    if command -v obs >/dev/null || { command -v flatpak >/dev/null && flatpak info com.obsproject.Studio >/dev/null 2>&1; }; then
        open_obs_when_server_is_up &
    else
        echo "VR_START_OBS=1 but OBS was not found: open it yourself"
    fi
fi

stop=0
trap 'stop=1' INT TERM
while [ "$stop" -eq 0 ]; do
    uv run run_server.py
    code=$?
    [ "$stop" -eq 1 ] && break
    [ "$code" -eq 0 ] && break
    echo "Server stopped (exit $code). Restarting in 5 seconds, Ctrl+C to quit."
    sleep 5
done
