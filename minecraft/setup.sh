#!/usr/bin/env bash
# One time setup for Minecraft mode (./go-live.sh --minecraft).
#   ./minecraft/setup.sh
# Downloads the official Minecraft server, asks you to accept the Minecraft
# EULA, and installs Mindcraft (the AI that plays as Mika and Luna).
# Safe to run again: finished steps are skipped.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
MC_VERSION="${MC_VERSION:-1.21.6}"
MINDCRAFT_REPO="https://github.com/mindcraft-bots/mindcraft.git"
MINDCRAFT_COMMIT="f6a9556cf756e6bd88a75cc2fa0d5aed7599b101"
PORT="${VR_MINECRAFT_PORT:-25565}"

say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1. $2"; exit 1; }; }

need java "Install it with: sudo apt install openjdk-21-jre-headless"
need node "Install Node 20 first."
need npm "Install Node 20 first."
need git "Install it with: sudo apt install git"
need python3 "Install it with: sudo apt install python3"

java_major="$(java -version 2>&1 | sed -nE 's/.*version "([0-9]+).*/\1/p' | head -1)"
if [ "${java_major:-0}" -lt 21 ]; then
    echo "Java 21 or newer is needed (found ${java_major:-none})."; exit 1
fi
node_major="$(node -v | sed -E 's/v([0-9]+).*/\1/')"
if [ "$node_major" -lt 18 ]; then
    echo "Node 18 or newer is needed (found $(node -v))."; exit 1
fi

# ---------------------------------------------------------------- server
mkdir -p server runtime
if [ ! -f server/server.jar ]; then
    say "Downloading the official Minecraft $MC_VERSION server from Mojang"
    python3 - "$MC_VERSION" <<'PY'
import hashlib, json, sys, urllib.request
version = sys.argv[1]
def get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read()
manifest = json.loads(get("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"))
entry = next((v for v in manifest["versions"] if v["id"] == version), None)
if not entry:
    sys.exit(f"Version {version} not found")
meta = json.loads(get(entry["url"]))
server = meta["downloads"]["server"]
data = get(server["url"])
if hashlib.sha1(data).hexdigest() != server["sha1"]:
    sys.exit("Download check failed, try again")
open("server/server.jar", "wb").write(data)
print(f"server.jar ok ({len(data) // 1024 // 1024} MB)")
PY
fi

if ! grep -qs '^eula=true' server/eula.txt; then
    say "Minecraft EULA"
    echo "Running a Minecraft server needs you to accept the Minecraft EULA:"
    echo "  https://aka.ms/MinecraftEULA"
    read -r -p "Type yes if you accept it: " answer
    if [ "$(echo "$answer" | tr '[:upper:]' '[:lower:]')" != "yes" ]; then
        echo "Not accepted, stopping here."; exit 1
    fi
    printf '#Accepted by the owner in minecraft/setup.sh\neula=true\n' > server/eula.txt
fi

if [ ! -f server/server.properties ]; then
    say "Writing server.properties (offline mode, only reachable from this PC)"
    cat > server/server.properties <<PROPS
motd=Mika and Luna play Minecraft
server-ip=127.0.0.1
server-port=$PORT
online-mode=false
enforce-secure-profile=false
gamemode=survival
difficulty=easy
spawn-protection=0
max-players=6
view-distance=8
simulation-distance=6
allow-flight=true
enable-command-block=false
level-name=world
PROPS
fi

# ---------------------------------------------------------------- mindcraft
if [ ! -d mindcraft/.git ]; then
    say "Downloading Mindcraft"
    git clone "$MINDCRAFT_REPO" mindcraft
fi
( cd mindcraft && git fetch -q origin "$MINDCRAFT_COMMIT" 2>/dev/null || true; git checkout -q "$MINDCRAFT_COMMIT" )
if [ ! -d mindcraft/node_modules ]; then
    say "Installing Mindcraft (a few minutes)"
    ( cd mindcraft && npm install --no-audit --no-fund )
fi

# ---------------------------------------------------------------- camera client
python3 add_server.py || true

say "Minecraft mode is ready"
echo "Go live with:  ./go-live.sh --minecraft"
echo "Your OpenAI key is read from .env (OPENAI_API_KEY), nothing is copied."
