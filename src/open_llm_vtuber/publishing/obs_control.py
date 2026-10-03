"""OBS, driven from the server (obs-websocket 5, built into OBS 28+).

Going live (VR_START_OBS=1), in this order:
  1. wait until OBS answers (go-live.sh opens it; it can take a while)
  2. find the Browser source showing the Stage (create one if there is none)
     and switch to the scene that has it
  3. reload that source WITHOUT cache, so OBS never shows an old page
  4. wait until the reloaded page has loaded Mika and Luna
  5. start streaming, then wait a few seconds for YouTube to pick it up
Only then does the class begin (STREAM_LIVE is set).

Ending: stop streaming and close OBS.

    OBS_WEBSOCKET_URL       ws://127.0.0.1:4455
    OBS_WEBSOCKET_PASSWORD  from Tools, WebSocket Server Settings
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import itertools
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from loguru import logger

STAGE_URL = "http://127.0.0.1:12393/vr-agent/teaching-stage.html"
# Set when the stream is really live (or immediately when OBS is not managed).
STREAM_LIVE = asyncio.Event()


def manage_obs() -> bool:
    return os.environ.get("VR_START_OBS", "0").strip().lower() in ("1", "true", "yes", "on")


class OBSError(RuntimeError):
    pass


# OBS keeps its WebSocket settings in a file on this same computer, so the
# password never has to be copied into .env (and can never be wrong).
CONFIG_PATHS = (
    Path.home() / ".config/obs-studio/plugin_config/obs-websocket/config.json",
    Path.home() / ".var/app/com.obsproject.Studio/config/obs-studio/plugin_config/obs-websocket/config.json",
)


def _obs_config() -> tuple[Optional[Path], dict[str, Any]]:
    for path in CONFIG_PATHS:
        try:
            return path, json.loads(path.read_text())
        except Exception:
            continue
    return None, {}


def obs_running() -> bool:
    return subprocess.run(["pgrep", "-x", "obs"], capture_output=True).returncode == 0


def prepare_obs_config() -> None:
    """Before OBS opens: make sure its WebSocket server is switched on."""
    path, config = _obs_config()
    if path is None or obs_running() or config.get("server_enabled"):
        return
    config["server_enabled"] = True
    try:
        path.write_text(json.dumps(config, indent=4))
        logger.info("OBS: WebSocket server switched on in OBS settings")
    except Exception as exc:
        logger.warning(f"OBS: could not switch the WebSocket server on: {exc}")


def launch_obs(streaming: bool) -> bool:
    """Open OBS from the terminal (minimized), optionally streaming at once."""
    args = ["--minimize-to-tray", "--disable-shutdown-check"] + (["--startstreaming"] if streaming else [])
    for command in (["obs"], ["flatpak", "run", "com.obsproject.Studio"]):
        if shutil.which(command[0]):
            try:
                subprocess.Popen(
                    command + args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,  # Ctrl+C in the terminal must not kill it
                )
                return True
            except Exception as exc:
                logger.warning(f"OBS: could not open: {exc}")
    return False


async def close_obs(timeout: float = 15) -> None:
    """Close OBS like clicking X (it stops the stream first)."""
    if not obs_running():
        return
    subprocess.run(["pkill", "-x", "obs"], check=False)
    deadline = time.time() + timeout
    while time.time() < deadline and obs_running():
        await asyncio.sleep(0.5)
    if obs_running():
        subprocess.run(["pkill", "-9", "-x", "obs"], check=False)
    logger.info("OBS: closed")


class OBS:
    """One authenticated obs-websocket connection: ``async with OBS() as obs``."""

    def __init__(self) -> None:
        _path, config = _obs_config()
        port = config.get("server_port") or 4455
        self.url = os.environ.get("OBS_WEBSOCKET_URL", "").strip() or f"ws://127.0.0.1:{port}"
        # OBS's own settings file wins: it is always the current password.
        self.password = str(config.get("server_password") or "") or os.environ.get(
            "OBS_WEBSOCKET_PASSWORD", ""
        ).strip()
        self.ws = None
        self._ids = itertools.count(1)

    async def __aenter__(self) -> "OBS":
        import websockets

        self.ws = await websockets.connect(self.url, open_timeout=5, max_size=8 * 1024 * 1024)
        hello = json.loads(await asyncio.wait_for(self.ws.recv(), 5))
        identify: dict[str, Any] = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth = (hello.get("d") or {}).get("authentication")
        if auth:
            secret = base64.b64encode(
                hashlib.sha256((self.password + auth["salt"]).encode()).digest()
            ).decode()
            identify["authentication"] = base64.b64encode(
                hashlib.sha256((secret + auth["challenge"]).encode()).digest()
            ).decode()
        await self.ws.send(json.dumps({"op": 1, "d": identify}))
        try:
            reply = json.loads(await asyncio.wait_for(self.ws.recv(), 5))
        except Exception as exc:
            if "4009" in str(exc):
                raise OBSError("OBS refused the WebSocket password") from None
            raise
        if reply.get("op") != 2:
            raise OBSError("OBS refused the WebSocket password")
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self.ws is not None:
            await self.ws.close()

    async def call(self, request_type: str, data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        request_id = str(next(self._ids))
        payload: dict[str, Any] = {"requestType": request_type, "requestId": request_id}
        if data:
            payload["requestData"] = data
        await self.ws.send(json.dumps({"op": 6, "d": payload}))
        while True:
            message = json.loads(await asyncio.wait_for(self.ws.recv(), 10))
            body = message.get("d") or {}
            if message.get("op") == 7 and body.get("requestId") == request_id:
                status = body.get("requestStatus") or {}
                if not status.get("result"):
                    raise OBSError(f"{request_type}: {status.get('comment') or status.get('code')}")
                return body.get("responseData") or {}


async def _connect_when_ready(wait_seconds: float) -> Optional[OBS]:
    deadline = time.time() + wait_seconds
    last = ""
    while time.time() < deadline:
        try:
            return await OBS().__aenter__()
        except OBSError as exc:
            logger.error(f"OBS: {exc}")
            return None
        except Exception as exc:
            last = str(exc)
            await asyncio.sleep(3)
    logger.warning(f"OBS: no answer after {wait_seconds:.0f}s ({last[:80]}). Is OBS open with its WebSocket server on?")
    return None


async def _stage_source(obs: OBS) -> tuple[str, str]:
    """(browser source name, scene that holds it). Creates the source when missing."""
    inputs = (await obs.call("GetInputList", {"inputKind": "browser_source"})).get("inputs") or []
    stage = ""
    for item in inputs:
        name = item.get("inputName", "")
        settings = (await obs.call("GetInputSettings", {"inputName": name})).get("inputSettings") or {}
        if "teaching-stage" in str(settings.get("url", "")):
            stage = name
            break
    scenes = (await obs.call("GetSceneList")).get("scenes") or []
    if stage:
        for scene in scenes:
            items = (await obs.call("GetSceneItemList", {"sceneName": scene["sceneName"]})).get("sceneItems") or []
            if any(i.get("sourceName") == stage for i in items):
                return stage, scene["sceneName"]
    # No Stage source anywhere: add one, full screen, to the current scene.
    scene = (await obs.call("GetCurrentProgramScene")).get("currentProgramSceneName") or (
        scenes[0]["sceneName"] if scenes else ""
    )
    stage = stage or "VR Stage"
    await obs.call(
        "CreateInput",
        {
            "sceneName": scene,
            "inputName": stage,
            "inputKind": "browser_source",
            "inputSettings": {
                "url": STAGE_URL,
                "width": 1920,
                "height": 1080,
                "reroute_audio": False,
                "shutdown": False,
            },
            "sceneItemEnabled": True,
        },
    )
    logger.info(f"OBS: added a Browser source '{stage}' with the Stage to scene '{scene}'")
    return stage, scene


async def _reload_stage(obs: OBS, stage: str, scene: str, hard: bool) -> None:
    """Reload the Stage without cache. hard: also hide and show the source,
    the same as clicking it off and on in OBS (fixes a page stuck on black)."""
    if hard:
        item = await obs.call("GetSceneItemId", {"sceneName": scene, "sourceName": stage})
        item_id = item.get("sceneItemId")
        await obs.call("SetSceneItemEnabled", {"sceneName": scene, "sceneItemId": item_id, "sceneItemEnabled": False})
        await asyncio.sleep(1.5)
        await obs.call("SetSceneItemEnabled", {"sceneName": scene, "sceneItemId": item_id, "sceneItemEnabled": True})
        await asyncio.sleep(0.5)
    await obs.call("PressInputPropertiesButton", {"inputName": stage, "propertyName": "refreshnocache"})


async def _ready_stage(obs: OBS, stage_loaded_since: Callable[[float], bool]) -> bool:
    """Scene with the Stage on air, page reloaded until Mika and Luna are drawn."""
    stage, scene = await _stage_source(obs)
    await obs.call("SetCurrentProgramScene", {"sceneName": scene})
    for attempt in range(4):
        reloaded_at = time.time()
        await _reload_stage(obs, stage, scene, hard=attempt > 0)
        logger.info(
            f"OBS: scene '{scene}', Stage reloaded without cache"
            + (" (hidden and shown again)" if attempt else "")
            + ", waiting for Mika and Luna"
        )
        for _ in range(25):
            await asyncio.sleep(1)
            if stage_loaded_since(reloaded_at):
                logger.info("OBS: the Stage is drawn")
                return True
        logger.warning("OBS: the Stage did not load in 25 s, reloading it again")
    return False


async def go_live(stage_loaded_since: Callable[[float], bool]) -> bool:
    """The whole start sequence. Sets STREAM_LIVE when the stream is up."""
    if not manage_obs():
        STREAM_LIVE.set()
        return False
    prepare_obs_config()
    if not obs_running():
        logger.info("OBS: opening it (minimized)")
        launch_obs(streaming=False)
    started = time.time()
    logger.info("OBS: waiting for OBS to answer (WebSocket server)")
    obs = await _connect_when_ready(90)
    if obs is None:
        # No remote control: restart OBS from the terminal, streaming at once.
        # The server is already up, so the Stage page loads straight away.
        logger.warning("OBS: no remote control, so OBS is reopened and told to stream from the terminal")
        await close_obs()
        if launch_obs(streaming=True):
            await asyncio.sleep(20)
            logger.info("OBS: reopened with streaming on")
        STREAM_LIVE.set()
        return False
    logger.info("OBS: connected")
    try:
        streaming = (await obs.call("GetStreamStatus")).get("outputActive")
        ready = await _ready_stage(obs, stage_loaded_since)
        if streaming:
            logger.info("OBS: already streaming (the Stage was reloaded)")
        else:
            if not ready:
                logger.error("OBS: the Stage never loaded, so the stream is NOT started. Check the Browser source.")
                return False
            await asyncio.sleep(2)  # first frames drawn
            await obs.call("StartStream")
            logger.info(f"OBS: streaming started ({time.time() - started:.0f}s after the server)")
    except Exception as exc:
        logger.warning(f"OBS: start sequence failed: {exc}")
        STREAM_LIVE.set()
        return False
    finally:
        await obs.__aexit__()
    await asyncio.sleep(8)  # YouTube needs a few seconds to show the stream
    STREAM_LIVE.set()
    return True


async def stop_and_close() -> None:
    """Stop streaming, then close OBS (only when VR_START_OBS manages it)."""
    try:
        async with OBS() as obs:
            status = await obs.call("GetStreamStatus")
            if status.get("outputActive"):
                await obs.call("StopStream")
                logger.info("OBS: streaming stopped")
                for _ in range(20):
                    await asyncio.sleep(0.5)
                    if not (await obs.call("GetStreamStatus")).get("outputActive"):
                        break
    except Exception as exc:
        logger.warning(f"OBS: could not stop streaming: {exc}")
    if manage_obs():
        try:
            await close_obs()
        except Exception as exc:
            logger.debug(f"OBS: close failed: {exc}")
