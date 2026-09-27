#!/usr/bin/env bash
# Adds "VR Agent" and "Stop VR Agent" to your apps menu (run once).
# After this, start everything from the menu or the dock: no terminal.
set -e
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"
chmod +x "$ROOT/launcher/start_vr_agent.sh" "$ROOT/launcher/stop_vr_agent.sh" "$ROOT/open_room.sh"
ICON="$ROOT/avatars/mao.png"

cat >"$APPS/vr-agent.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=VR Agent
Comment=Start the VR Agent server and open OBS
Exec="$ROOT/launcher/start_vr_agent.sh"
Icon=$ICON
Terminal=false
Categories=AudioVideo;Video;
DESKTOP

cat >"$APPS/vr-agent-stop.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Stop VR Agent
Comment=Stop the VR Agent server
Exec="$ROOT/launcher/stop_vr_agent.sh"
Icon=$ICON
Terminal=false
Categories=AudioVideo;Video;
DESKTOP

chmod +x "$APPS/vr-agent.desktop" "$APPS/vr-agent-stop.desktop"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" >/dev/null 2>&1 || true
echo "Done. Look for \"VR Agent\" in your apps menu."
