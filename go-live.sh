#!/usr/bin/env bash
# Go live from a plain terminal (no VS Code needed):
#   ./go-live.sh --class   or   ./go-live.sh --minecraft
# Starts the server, and if it ever crashes it starts again by itself.
# Press Ctrl+C to stop for real.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

# ./go-live.sh --class       Natori and Hibiki teach; continues where the last class stopped
# ./go-live.sh --minecraft   Mika and Luna play Minecraft (run ./minecraft/setup.sh once first)
# ./go-live.sh --fresh       (with --class) starts the courses again from lesson 1
# Nothing to change in .env to switch between them.
for arg in "$@"; do
    case "$arg" in
        --class|class)
            export VR_MODE=class VR_CLASS_MODE=1 VR_ROOM_CAST="${VR_ROOM_CAST:-natori,hibiki}"
            echo "Class mode: Natori and Hibiki teach, continuing where the last class stopped"
            ;;
        --fresh)
            rm -f data/class/progress.json data/class/current_lesson.json
            echo "Class progress cleared: starting from lesson 1"
            ;;
        --minecraft|minecraft)
            export VR_MODE=minecraft VR_CLASS_MODE=0 VR_ROOM_CAST="${VR_ROOM_CAST:-mika,luna}"
            if [ ! -f minecraft/server/server.jar ] || [ ! -d minecraft/mindcraft/node_modules ]; then
                echo "Minecraft is not set up yet. Run this once first:  ./minecraft/setup.sh"
                exit 1
            fi
            echo "Minecraft mode: Mika and Luna play, chat steers them"
            ;;
    esac
done

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

# VR_START_OBS=1 in .env: the server opens OBS, picks the Stage scene, reloads
# the page without cache, waits for Mika and Luna, then starts streaming.
# Ctrl+C: goodbye, the broadcast ends, OBS stops and closes.
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

# The Minecraft server and the bots stop with the stream (the world is saved).
if [ "${VR_MODE:-}" = "minecraft" ]; then
    ./minecraft/stop.sh
fi

# Whatever happened above, OBS never stays open streaming on its own.
if grep -qE '^VR_START_OBS=(1|true)' .env 2>/dev/null && pgrep -x obs >/dev/null; then
    echo "Closing OBS"
    pkill -x obs
fi
