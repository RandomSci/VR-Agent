#!/usr/bin/env bash
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # the DEV copy this script lives in
STAGE_URL="http://127.0.0.1:12399/vr-agent/teaching-stage.html"
LOG="/tmp/vr-full-dev.log"
PID_FILE="/tmp/vr-full-dev.pid"
FEED_PID_FILE="/tmp/vr-n8n-feed.pid"

cd "$ROOT" || exit 1

# --------------------------------------------------
# CODING RUNTIME
#
# Inherit the Python environment that launched this
# script. In development this should be the active
# Conda environment: For_AI.
# --------------------------------------------------

export VR_CODING_PYTHON="$(python -c 'import sys; print(sys.executable)')"
export VR_CODING_PYTHON_PREFIX="$(python -c 'import sys; print(sys.prefix)')"
export VR_CODING_CONDA_ENV="${CONDA_DEFAULT_ENV:-}"

echo "Coding Python: $VR_CODING_PYTHON"
echo "Coding prefix: $VR_CODING_PYTHON_PREFIX"
echo "Conda env: ${VR_CODING_CONDA_ENV:-none}"

# Libraries Mika can use in Python. Missing ones only limit what she can
# build (a seaborn chart fails), so warn and carry on.
missing="$("$VR_CODING_PYTHON" - <<'PY'
import importlib.util
need = {"numpy": "numpy", "matplotlib": "matplotlib", "seaborn": "seaborn", "scipy": "scipy",
        "sympy": "sympy", "pandas": "pandas", "PIL": "pillow", "scienceplots": "SciencePlots"}
print(" ".join(pkg for module, pkg in need.items() if importlib.util.find_spec(module) is None))
PY
)"
if [ -n "$missing" ]; then
    echo "Coding Python is missing: $missing"
    echo "  Install once with: $VR_CODING_PYTHON -m pip install $missing"
fi


# --------------------------------------------------
# STAGE WITH SOUND
#
# A normal browser tab blocks audio until you click
# the page, so Mika and Luna move their mouths in
# silence. LIVE never hits this because OBS and
# open_room.sh allow autoplay. Do the same here: open
# the Stage in its own window with autoplay allowed.
# --------------------------------------------------
open_stage_with_sound() {
    local url="$1"
    local chrome_flags=(--autoplay-policy=no-user-gesture-required
        "--user-data-dir=${HOME}/.vr-agent-stage-browser" --no-first-run
        --window-size=1920,1080 "--app=${url}")
    local app
    for app in google-chrome google-chrome-stable chromium chromium-browser microsoft-edge brave-browser; do
        if command -v "$app" >/dev/null 2>&1; then
            echo "Stage opens in $app with sound allowed."
            "$app" "${chrome_flags[@]}" >/dev/null 2>&1 &
            return 0
        fi
    done
    if command -v flatpak >/dev/null 2>&1; then
        for app in com.google.Chrome org.chromium.Chromium com.brave.Browser; do
            if flatpak info "$app" >/dev/null 2>&1; then
                echo "Stage opens in $app (Flatpak) with sound allowed."
                flatpak run "$app" "${chrome_flags[@]}" >/dev/null 2>&1 &
                return 0
            fi
        done
    fi
    for app in firefox firefox-esr; do
        if command -v "$app" >/dev/null 2>&1; then
            # A separate Firefox profile whose only job is allowing autoplay.
            local profile="${HOME}/.vr-agent-stage-firefox"
            mkdir -p "$profile"
            cat >"$profile/user.js" <<'PREFS'
user_pref("media.autoplay.default", 0);
user_pref("media.autoplay.blocking_policy", 0);
user_pref("media.autoplay.block-webaudio", false);
user_pref("browser.shell.checkDefaultBrowser", false);
PREFS
            echo "Stage opens in $app with sound allowed."
            "$app" --no-remote --profile "$profile" --new-window "$url" >/dev/null 2>&1 &
            return 0
        fi
    done
    echo "No Chrome, Chromium or Firefox found. Opening your default browser:"
    echo "  CLICK THE STAGE ONCE or you will not hear Mika and Luna."
    xdg-open "$url" >/dev/null 2>&1 &
}

if [ -f "$FEED_PID_FILE" ]; then
    feed_pid="$(cat "$FEED_PID_FILE")"
    feed_cmd="$(ps -p "$feed_pid" -o args= 2>/dev/null || true)"
    if [[ "$feed_cmd" == *"integrations/n8n_teacher/cdp_screencast_bridge.js"* ]]; then
        kill "$feed_pid" 2>/dev/null || true
    fi
    rm -f "$FEED_PID_FILE"
fi

if [ -f "$PID_FILE" ]; then
    old_pid="$(cat "$PID_FILE")"
    old_cmd="$(ps -p "$old_pid" -o args= 2>/dev/null || true)"
    if [[ "$old_cmd" == *"tests.harness.full_dev_server"* ]]; then
        echo "Stopping previous DEV server..."
        kill "$old_pid" 2>/dev/null || true
        for i in {1..20}; do
            kill -0 "$old_pid" 2>/dev/null || break
            sleep 0.25
        done
    fi
    rm -f "$PID_FILE"
fi

if curl -sf http://127.0.0.1:12399/ >/dev/null 2>&1; then
    echo "DEV still occupies port 12399. Stop that server before retrying."
    exit 1
fi

echo "Starting VR-Agent DEV..."
nohup uv run python -m tests.harness.full_dev_server >"$LOG" 2>&1 &
echo $! >"$PID_FILE"

for i in {1..60}; do
    curl -sf http://127.0.0.1:12399/ >/dev/null 2>&1 && break
    sleep 0.5
done

if ! curl -sf http://127.0.0.1:12399/ >/dev/null 2>&1; then
    echo "DEV failed to start:"
    tail -30 "$LOG"
    exit 1
fi

echo "DEV ready. Opening coding Stage..."
open_stage_with_sound "$STAGE_URL"
echo "Stage: $STAGE_URL"
grep "DEV voice\|voice unavailable" "$LOG" | sed 's/.*| //' || true
echo "Starting fake livestream chat..."
sleep 1
exec uv run python tests/harness/mock_chat_console.py
