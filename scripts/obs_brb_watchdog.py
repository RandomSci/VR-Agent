"""OBS be-right-back watchdog for VR Agent.

Switches OBS to a "BRB" scene whenever the VR Agent server is down (for
example while you restart it after changing code), or when you pause it with
POST /vr-agent/pause. When the server is back and the livestream page has
reconnected, it switches back to your live scene. Viewers never see a
broken or blank page.

OBS setup (once)
  1. Tools > WebSocket Server Settings > Enable WebSocket server. Note the
     port (4455) and password.
  2. Keep your live scene (with the VR Agent Browser source) and add a scene
     called "BRB" with an Image source pointing at frontend/vr-agent/brb.jpg.

Run next to the server, in a second terminal:
  uv run python scripts/obs_brb_watchdog.py --live-scene "Scene" --brb-scene "BRB" \
      --browser-source "Browser" --password YOUR_OBS_WEBSOCKET_PASSWORD

The password can also come from the OBS_WEBSOCKET_PASSWORD environment
variable. The watchdog only switches between those two scenes, so if you move
to another scene yourself (a "Starting soon" screen, say) it leaves you alone.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import itertools
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx
import websockets


class OBSError(Exception):
    pass


class OBSClient:
    """Minimal obs-websocket v5 client (built into OBS 28 and newer)."""

    def __init__(self, url: str, password: str = ""):
        self.url = url
        self.password = password
        self.ws = None
        self._ids = itertools.count(1)

    async def connect(self) -> None:
        self.ws = await websockets.connect(self.url, max_size=None, open_timeout=5)
        hello = json.loads(await self.ws.recv())
        if hello.get("op") != 0:
            raise OBSError("unexpected hello from OBS")
        identify: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth = hello.get("d", {}).get("authentication")
        if auth:
            if not self.password:
                raise OBSError("OBS WebSocket needs a password (--password)")
            secret = base64.b64encode(
                hashlib.sha256((self.password + auth["salt"]).encode()).digest()
            ).decode()
            identify["authentication"] = base64.b64encode(
                hashlib.sha256((secret + auth["challenge"]).encode()).digest()
            ).decode()
        await self.ws.send(json.dumps({"op": 1, "d": identify}))
        reply = json.loads(await self.ws.recv())
        if reply.get("op") != 2:
            raise OBSError("OBS refused the connection (wrong password?)")

    async def close(self) -> None:
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.ws = None

    async def request(self, request_type: str, data: Optional[dict] = None) -> dict:
        if self.ws is None:
            raise OBSError("not connected")
        request_id = str(next(self._ids))
        await self.ws.send(
            json.dumps(
                {
                    "op": 6,
                    "d": {
                        "requestType": request_type,
                        "requestId": request_id,
                        "requestData": data or {},
                    },
                }
            )
        )
        while True:
            message = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=5))
            if message.get("op") == 7 and message["d"].get("requestId") == request_id:
                status = message["d"].get("requestStatus", {})
                if not status.get("result"):
                    raise OBSError(
                        f"{request_type} failed: {status.get('comment', status)}"
                    )
                return message["d"].get("responseData") or {}

    async def current_scene(self) -> str:
        data = await self.request("GetCurrentProgramScene")
        return data.get("currentProgramSceneName") or data.get("sceneName") or ""

    async def set_scene(self, name: str) -> None:
        await self.request("SetCurrentProgramScene", {"sceneName": name})

    async def refresh_browser(self, input_name: str) -> None:
        await self.request(
            "PressInputPropertiesButton",
            {"inputName": input_name, "propertyName": "refreshnocache"},
        )


@dataclass
class Health:
    up: bool
    paused: bool = False
    live_clients: int = 0


async def check_health(client: httpx.AsyncClient, server: str) -> Health:
    try:
        response = await client.get(f"{server}/vr-agent/health", timeout=2.0)
        data = response.json()
        return Health(
            True, bool(data.get("paused")), int(data.get("livestream_clients", 0))
        )
    except Exception:
        return Health(False)


class Watchdog:
    def __init__(self, args, obs: Optional[OBSClient] = None, health_check=None):
        self.args = args
        self.obs = obs or OBSClient(args.obs, args.password)
        self.health_check = health_check
        self.down_checks = 0
        self.last_refresh = 0.0
        self.connected = False

    def log(self, message: str) -> None:
        print(time.strftime("%H:%M:%S"), message, flush=True)

    def target_scene(self, health: Health) -> Optional[str]:
        if not health.up:
            self.down_checks += 1
            # One missed check can be a hiccup; two in a row is a real outage.
            return self.args.brb_scene if self.down_checks >= 2 else None
        self.down_checks = 0
        if health.paused:
            return self.args.brb_scene
        if health.live_clients == 0:
            # Server is back but the OBS page has not reconnected yet.
            return self.args.brb_scene
        return self.args.live_scene

    async def step(self, http: httpx.AsyncClient) -> None:
        if not self.connected:
            await self.obs.connect()
            self.connected = True
            self.log("Connected to OBS.")
        health = await (
            self.health_check()
            if self.health_check
            else check_health(http, self.args.server)
        )
        target = self.target_scene(health)
        if (
            health.up
            and not health.paused
            and health.live_clients == 0
            and self.args.browser_source
            and time.time() - self.last_refresh > 8
        ):
            self.last_refresh = time.time()
            try:
                await self.obs.refresh_browser(self.args.browser_source)
                self.log("Server is back; refreshing the OBS browser source.")
            except OBSError as exc:
                self.log(f"Could not refresh browser source: {exc}")
        if target is None:
            return
        current = await self.obs.current_scene()
        if current == target or current not in (
            self.args.live_scene,
            self.args.brb_scene,
        ):
            return
        await self.obs.set_scene(target)
        reason = (
            "server down"
            if not health.up
            else "paused"
            if health.paused
            else "waiting for page"
            if health.live_clients == 0
            else "server back"
        )
        self.log(f"Switched OBS to '{target}' ({reason}).")

    async def run(self) -> None:
        async with httpx.AsyncClient() as http:
            backoff = 2.0
            while True:
                try:
                    await self.step(http)
                    backoff = 2.0
                except (
                    OSError,
                    OBSError,
                    websockets.WebSocketException,
                    asyncio.TimeoutError,
                ) as exc:
                    if self.connected:
                        self.log(f"OBS connection lost ({exc}); retrying.")
                    else:
                        self.log(
                            f"Cannot reach OBS WebSocket ({exc}); retrying in {backoff:.0f}s."
                        )
                    self.connected = False
                    await self.obs.close()
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue
                await asyncio.sleep(self.args.interval)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Switch OBS to a BRB scene while VR Agent is down."
    )
    parser.add_argument(
        "--live-scene", required=True, help="OBS scene with the VR Agent browser source"
    )
    parser.add_argument(
        "--brb-scene", default="BRB", help="OBS scene with the be-right-back image"
    )
    parser.add_argument(
        "--browser-source",
        default="",
        help="Name of the VR Agent Browser source, refreshed after restarts",
    )
    parser.add_argument(
        "--password", default=os.environ.get("OBS_WEBSOCKET_PASSWORD", "")
    )
    parser.add_argument(
        "--obs", default="ws://127.0.0.1:4455", help="OBS WebSocket address"
    )
    parser.add_argument(
        "--server", default="http://127.0.0.1:12393", help="VR Agent server address"
    )
    parser.add_argument(
        "--interval", type=float, default=2.0, help="Seconds between checks"
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    print("VR Agent OBS watchdog running. Ctrl+C to stop.", flush=True)
    try:
        asyncio.run(Watchdog(args).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
