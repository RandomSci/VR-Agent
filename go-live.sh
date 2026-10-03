#!/usr/bin/env bash
# Go live from a plain terminal (no VS Code needed):
#   ./go-live.sh
# Starts the server, and if it ever crashes it starts again by itself.
# Press Ctrl+C to stop for real.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

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
