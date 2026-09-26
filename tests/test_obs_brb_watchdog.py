"""OBS watchdog against a fake obs-websocket v5 server (with password auth)."""

import asyncio
import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

websockets = pytest.importorskip("websockets")

spec = importlib.util.spec_from_file_location(
    "obs_brb_watchdog",
    Path(__file__).resolve().parents[1] / "scripts" / "obs_brb_watchdog.py",
)
wd = importlib.util.module_from_spec(spec)
sys.modules["obs_brb_watchdog"] = wd  # dataclasses need the module registered
spec.loader.exec_module(wd)

PASSWORD = "secret"
SALT, CHALLENGE = "salt123", "chal456"


class FakeOBS:
    def __init__(self):
        self.scene = "Live"
        self.calls = []

    async def handler(self, ws):
        await ws.send(
            json.dumps(
                {
                    "op": 0,
                    "d": {
                        "rpcVersion": 1,
                        "authentication": {"salt": SALT, "challenge": CHALLENGE},
                    },
                }
            )
        )
        ident = json.loads(await ws.recv())
        secret = base64.b64encode(
            hashlib.sha256((PASSWORD + SALT).encode()).digest()
        ).decode()
        expected = base64.b64encode(
            hashlib.sha256((secret + CHALLENGE).encode()).digest()
        ).decode()
        if ident["d"].get("authentication") != expected:
            await ws.close()
            return
        await ws.send(json.dumps({"op": 2, "d": {"negotiatedRpcVersion": 1}}))
        async for raw in ws:
            msg = json.loads(raw)["d"]
            self.calls.append((msg["requestType"], msg["requestData"]))
            data = {}
            if msg["requestType"] == "GetCurrentProgramScene":
                data = {"currentProgramSceneName": self.scene}
            elif msg["requestType"] == "SetCurrentProgramScene":
                self.scene = msg["requestData"]["sceneName"]
            await ws.send(
                json.dumps(
                    {
                        "op": 7,
                        "d": {
                            "requestId": msg["requestId"],
                            "requestStatus": {"result": True, "code": 100},
                            "responseData": data,
                        },
                    }
                )
            )


def test_switches_to_brb_when_server_is_down_and_back_when_page_reconnects():
    fake = FakeOBS()
    states = iter(
        [
            wd.Health(True, False, 1),  # healthy, stays live
            wd.Health(False),  # one miss, no switch yet
            wd.Health(False),  # outage, BRB
            wd.Health(
                True, False, 0
            ),  # server back, page not connected: refresh, stay BRB
            wd.Health(True, False, 1),  # page connected, back to live
            wd.Health(True, True, 1),  # paused, BRB
        ]
    )
    seen_scenes = []

    async def health():
        return next(states)

    async def scenario():
        async with websockets.serve(fake.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            args = wd.parse_args(
                [
                    "--live-scene",
                    "Live",
                    "--brb-scene",
                    "BRB",
                    "--browser-source",
                    "Page",
                    "--password",
                    PASSWORD,
                    "--obs",
                    f"ws://127.0.0.1:{port}",
                ]
            )
            dog = wd.Watchdog(args, health_check=health)
            async with wd.httpx.AsyncClient() as http:
                for _ in range(6):
                    await dog.step(http)
                    seen_scenes.append(fake.scene)
            await dog.obs.close()

    asyncio.run(scenario())
    assert seen_scenes == ["Live", "Live", "BRB", "BRB", "Live", "BRB"]
    assert (
        "PressInputPropertiesButton",
        {"inputName": "Page", "propertyName": "refreshnocache"},
    ) in fake.calls


def test_leaves_other_scenes_alone():
    fake = FakeOBS()
    fake.scene = "Starting soon"

    async def health():
        return wd.Health(False)

    async def scenario():
        async with websockets.serve(fake.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            args = wd.parse_args(
                [
                    "--live-scene",
                    "Live",
                    "--password",
                    PASSWORD,
                    "--obs",
                    f"ws://127.0.0.1:{port}",
                ]
            )
            dog = wd.Watchdog(args, health_check=health)
            async with wd.httpx.AsyncClient() as http:
                for _ in range(3):
                    await dog.step(http)
            await dog.obs.close()

    asyncio.run(scenario())
    assert fake.scene == "Starting soon"


def test_wrong_password_is_reported():
    fake = FakeOBS()

    async def scenario():
        async with websockets.serve(fake.handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            client = wd.OBSClient(f"ws://127.0.0.1:{port}", "nope")
            with pytest.raises((wd.OBSError, websockets.WebSocketException)):
                await client.connect()
            await client.close()

    asyncio.run(scenario())
