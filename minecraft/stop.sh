#!/usr/bin/env bash
# Stops the Minecraft server and Mindcraft if they are still running.
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 0
for name in mindcraft server; do
    f="runtime/$name.pid"
    [ -f "$f" ] || continue
    pid="$(cat "$f")"
    if kill -0 "$pid" 2>/dev/null; then
        echo "Stopping Minecraft $name"
        kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
        for _ in $(seq 1 60); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
        kill -KILL -- "-$pid" 2>/dev/null || true
    fi
    rm -f "$f"
done
