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


# OBS keeps its settings in files on this same computer. Since OBS 30 the
# WebSocket password lives in user.ini (older: global.ini, then config.json),
# so it is read from there and never has to be copied into .env.
OBS_DIRS = (
    Path.home() / ".config/obs-studio",
    Path.home() / ".var/app/com.obsproject.Studio/config/obs-studio",
)


def _ini(path: Path):
    import configparser

    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # OBS keys are CamelCase
    parser.read(path, encoding="utf-8-sig")
    return parser


def _obs_dir() -> Optional[Path]:
    """The settings folder of the OBS actually in use: with both the normal and
    the Flatpak OBS installed, the one OBS saved to most recently."""
    def last_used(d: Path) -> float:
        times = [p.stat().st_mtime for p in (d / "user.ini", d / "global.ini") if p.is_file()]
        return max(times or [0.0])

    found = [d for d in OBS_DIRS if d.is_dir()]
    return max(found, key=last_used) if found else None


def _settings_ini() -> Optional[Path]:
    base = _obs_dir()
    if base is None:
        return None
    for name in ("user.ini", "global.ini"):
        path = base / name
        if path.is_file() and _ini(path).has_section("OBSWebSocket"):
            return path
    for name in ("user.ini", "global.ini"):
        if (base / name).is_file():
            return base / name
    return None


def _obs_config() -> tuple[Optional[Path], dict[str, Any]]:
    """{server_enabled, server_port, auth_required, server_password}."""
    path = _settings_ini()
    if path is not None:
        ini = _ini(path)
        if ini.has_section("OBSWebSocket"):
            sec = ini["OBSWebSocket"]
            return path, {
                "server_enabled": sec.get("ServerEnabled", "false").lower() == "true",
                "server_port": int(sec.get("ServerPort", "4455") or 4455),
                "auth_required": sec.get("AuthRequired", "true").lower() == "true",
                "server_password": sec.get("ServerPassword", ""),
            }
    base = _obs_dir()
    legacy = base / "plugin_config/obs-websocket/config.json" if base else None
    try:
        return legacy, json.loads(legacy.read_text())  # type: ignore[union-attr]
    except Exception:
        return None, {}


def obs_running() -> bool:
    return subprocess.run(["pgrep", "-x", "obs"], capture_output=True).returncode == 0


def _write_ini(path: Path, ini) -> None:
    with path.open("w", encoding="utf-8") as handle:
        ini.write(handle, space_around_delimiters=False)


def prepare_obs_config(stream_key: str = "") -> None:
    """Before OBS opens: WebSocket server on, and the YouTube stream key in
    the current profile (so Start Streaming never asks to pick a broadcast)."""
    if obs_running():
        return
    # Wherever this OBS version keeps the WebSocket settings (user.ini or
    # global.ini on new versions, config.json on old ones): server on, and the
    # password from .env when there is one, so both sides always agree.
    password = os.environ.get("OBS_WEBSOCKET_PASSWORD", "").strip()
    for base in (d for d in OBS_DIRS if d.is_dir()):
        _prepare_websocket(base, password)
    if stream_key:
        _write_stream_key(stream_key)
    _write_stream_quality()


def _stream_quality() -> tuple[int, int]:
    """(frames per second, video kbps). 30 fps and 6000 kbps: smooth 1080p on
    YouTube, half the work of 60 fps for an integrated graphics chip."""
    def number(name: str, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(os.environ.get(name, "") or default)))
        except ValueError:
            return default

    return number("VR_OBS_FPS", 30, 10, 60), number("VR_OBS_BITRATE", 6000, 1500, 20000)


def _write_stream_quality() -> None:
    """The same settings in the profile file, for an OBS opened from the terminal."""
    folder = _profile_dir()
    path = folder / "basic.ini" if folder else None
    if not (path and path.is_file()):
        return
    fps, kbps = _stream_quality()
    try:
        ini = _ini(path)
        for section in ("Video", "SimpleOutput"):
            if not ini.has_section(section):
                ini.add_section(section)
        ini["Video"]["FPSType"] = "0"
        ini["Video"]["FPSCommon"] = str(fps)
        ini["SimpleOutput"]["VBitrate"] = str(kbps)
        _write_ini(path, ini)
    except Exception as exc:
        logger.debug(f"OBS: stream quality not written: {exc}")


async def _tune_obs(obs: "OBS") -> None:
    """Before streaming starts: frame rate and bitrate (settings OBS keeps)."""
    fps, kbps = _stream_quality()
    try:
        video = await obs.call("GetVideoSettings")
        current = round(video.get("fpsNumerator", 0) / max(1, video.get("fpsDenominator", 1)))
        if current != fps:
            await obs.call("SetVideoSettings", {"fpsNumerator": fps, "fpsDenominator": 1})
            logger.info(f"OBS: frame rate set to {fps} fps")
    except Exception as exc:
        logger.debug(f"OBS: frame rate not changed: {exc}")
    try:
        await obs.call(
            "SetProfileParameter",
            {"parameterCategory": "SimpleOutput", "parameterName": "VBitrate", "parameterValue": str(kbps)},
        )
    except Exception as exc:
        logger.debug(f"OBS: bitrate not changed: {exc}")


def _prepare_websocket(base: Path, password: str) -> None:
    for name in ("user.ini", "global.ini"):
        path = base / name
        if not (path and path.is_file()):
            continue
        ini = _ini(path)
        if not ini.has_section("OBSWebSocket"):
            continue
        sec = ini["OBSWebSocket"]
        changed = sec.get("ServerEnabled", "").lower() != "true" or (password and sec.get("ServerPassword") != password)
        if changed:
            sec["ServerEnabled"] = "true"
            if password:
                sec["ServerPassword"] = password
                sec["AuthRequired"] = "true"
            try:
                _write_ini(path, ini)
                logger.info(f"OBS: WebSocket settings checked ({name})")
            except Exception as exc:
                logger.warning(f"OBS: could not update {name}: {exc}")
    legacy = base / "plugin_config/obs-websocket/config.json"
    if legacy.is_file():
        try:
            config = json.loads(legacy.read_text())
            if not config.get("server_enabled") or (password and config.get("server_password") != password):
                config["server_enabled"] = True
                if password:
                    config["server_password"] = password
                    config["auth_required"] = True
                legacy.write_text(json.dumps(config, indent=4))
                logger.info(f"OBS: WebSocket settings checked ({legacy})")
        except Exception as exc:
            logger.warning(f"OBS: could not update config.json: {exc}")


def _profile_dir() -> Optional[Path]:
    base = _obs_dir()
    path = _settings_ini()
    if base is None or path is None:
        return None
    ini = _ini(path)
    name = ini.get("Basic", "ProfileDir", fallback="") or ini.get("Basic", "Profile", fallback="")
    folder = base / "basic/profiles" / name if name else None
    if folder and folder.is_dir():
        return folder
    profiles = sorted((base / "basic/profiles").glob("*/"))
    return profiles[0] if len(profiles) == 1 else None


def _write_stream_key(key: str) -> None:
    folder = _profile_dir()
    if folder is None:
        logger.warning("OBS: profile folder not found, the stream key was not set")
        return
    service = {"type": "rtmp_common", "settings": {"service": "YouTube - RTMPS", "server": "auto", "key": key}}
    try:
        (folder / "service.json").write_text(json.dumps(service, indent=4))
        logger.info(f"OBS: YouTube stream key set in profile '{folder.name}'")
    except Exception as exc:
        logger.warning(f"OBS: could not set the stream key: {exc}")


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

    def __init__(self, password: Optional[str] = None) -> None:
        _path, config = _obs_config()
        port = config.get("server_port") or 4455
        self.url = os.environ.get("OBS_WEBSOCKET_URL", "").strip() or f"ws://127.0.0.1:{port}"
        self.password = password if password is not None else (_passwords() or [""])[0]
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


def _passwords() -> list[str]:
    """Every password worth trying: .env first, then OBS's own settings files."""
    found = [os.environ.get("OBS_WEBSOCKET_PASSWORD", "").strip()]
    # Every OBS install: the normal one and the Flatpak one can both exist,
    # and the running OBS may be either.
    for base in (d for d in OBS_DIRS if d.is_dir()):
        for name in ("user.ini", "global.ini"):
            path = base / name
            if path.is_file():
                found.append(_ini(path).get("OBSWebSocket", "ServerPassword", fallback=""))
        try:
            found.append(json.loads((base / "plugin_config/obs-websocket/config.json").read_text()).get("server_password", ""))
        except Exception:
            pass
    return list(dict.fromkeys(p for p in found if p)) or [""]


def _config_report() -> str:
    """Where OBS keeps its WebSocket settings and whether they match .env.
    Never prints a password."""
    env = os.environ.get("OBS_WEBSOCKET_PASSWORD", "").strip()
    parts = []
    for base in OBS_DIRS:
        if not base.is_dir():
            continue
        for name in ("user.ini", "global.ini"):
            path = base / name
            if path.is_file() and _ini(path).has_section("OBSWebSocket"):
                sec = _ini(path)["OBSWebSocket"]
                same = "same as .env" if env and sec.get("ServerPassword") == env else "NOT the .env password"
                parts.append(f"{path} (on={sec.get('ServerEnabled')}, auth={sec.get('AuthRequired')}, {same})")
        legacy = base / "plugin_config/obs-websocket/config.json"
        if legacy.is_file():
            try:
                cfg = json.loads(legacy.read_text())
                same = "same as .env" if env and cfg.get("server_password") == env else "NOT the .env password"
                parts.append(f"{legacy} (on={cfg.get('server_enabled')}, {same})")
            except Exception:
                parts.append(f"{legacy} (unreadable)")
    return "; ".join(parts) or "no OBS WebSocket settings found"


async def _connect_when_ready(wait_seconds: float) -> Optional[OBS]:
    deadline = time.time() + wait_seconds
    last = ""
    refused_since = 0.0
    while time.time() < deadline:
        refused = 0
        candidates = _passwords()
        for password in candidates:
            try:
                return await OBS(password).__aenter__()
            except OBSError:
                refused += 1
            except Exception as exc:
                last = str(exc)
                break
        if refused == len(candidates):
            # A freshly opened OBS can answer before its settings are loaded:
            # keep trying for 20 s before giving up on the password.
            refused_since = refused_since or time.time()
            if time.time() - refused_since > 20:
                logger.error("OBS: OBS refused every known WebSocket password (check OBS_WEBSOCKET_PASSWORD)")
                logger.error(f"OBS: WebSocket settings found: {_config_report()}")
                return None
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


async def youtube_stream_key() -> str:
    """YOUTUBE_STREAM_KEY from .env, else the channel's default key from the
    YouTube API (same sign-in as the rest). Never logged."""
    key = os.environ.get("YOUTUBE_STREAM_KEY", "").strip()
    if key:
        return key
    try:
        from .settings import PublishSettings
        from .youtube import YouTubeClient

        s = PublishSettings.from_env(read_dotenv=False)
        if not s.youtube_ready:
            return ""
        client = YouTubeClient(s.youtube_client_id, s.youtube_client_secret, s.youtube_refresh_token)
        keys = await asyncio.to_thread(client.stream_keys)
    except Exception as exc:
        logger.warning(f"OBS: could not read the YouTube stream key: {str(exc)[:120]}")
        return ""
    if not keys:
        return ""
    chosen = next((k for k in keys if "default" in k["title"].lower()), keys[0])
    logger.info(f"OBS: using the YouTube stream key '{chosen['title']}'")
    return chosen["key"]


async def go_live(stage_loaded_since: Callable[[float], bool]) -> bool:
    """The whole start sequence. Sets STREAM_LIVE when the stream is up."""
    if not manage_obs():
        STREAM_LIVE.set()
        return False
    stream_key = await youtube_stream_key()
    prepare_obs_config(stream_key)
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
        await _terminal_stream(stream_key)
        return False
    logger.info("OBS: connected")
    try:
        if stream_key:
            # Stream key mode: Start Streaming goes straight to YouTube, no
            # "Select stream" step from the account connection.
            try:
                await obs.call(
                    "SetStreamServiceSettings",
                    {
                        "streamServiceType": "rtmp_common",
                        "streamServiceSettings": {"service": "YouTube - RTMPS", "server": "auto", "key": stream_key},
                    },
                )
            except Exception as exc:
                logger.debug(f"OBS: stream key not applied now: {exc}")
        streaming = (await obs.call("GetStreamStatus")).get("outputActive")
        if not streaming:
            await _tune_obs(obs)
        ready = await _ready_stage(obs, stage_loaded_since)
        if streaming:
            logger.info("OBS: already streaming (the Stage was reloaded)")
        else:
            if not ready:
                logger.error("OBS: the Stage never loaded, so the stream is NOT started. Check the Browser source.")
                return False
            await asyncio.sleep(2)  # first frames drawn
            await obs.call("StartStream")
            if await _really_streaming(obs):
                logger.info(f"OBS: streaming started ({time.time() - started:.0f}s after the server)")
            else:
                # OBS said yes but nothing reached YouTube: try once more with
                # the YouTube address written out, then the terminal way.
                logger.warning("OBS: Start Streaming did not connect to YouTube, trying again")
                try:
                    await obs.call("StopStream")
                except Exception:
                    pass
                await asyncio.sleep(2)
                if stream_key:
                    await obs.call(
                        "SetStreamServiceSettings",
                        {"streamServiceType": "rtmp_custom", "streamServiceSettings": {"server": YOUTUBE_RTMPS, "key": stream_key}},
                    )
                await obs.call("StartStream")
                if not await _really_streaming(obs):
                    raise OBSError("OBS could not connect to YouTube twice")
                logger.info("OBS: streaming started on the second try")
    except Exception as exc:
        logger.warning(f"OBS: start sequence failed ({exc}), reopening OBS to stream from the terminal")
        try:
            await obs.__aexit__()
        except Exception:
            pass
        await _terminal_stream(stream_key)
        return False
    finally:
        try:
            await obs.__aexit__()
        except Exception:
            pass
    await asyncio.sleep(8)  # YouTube needs a few seconds to show the stream
    STREAM_LIVE.set()
    _keep_task(asyncio.create_task(ensure_youtube_live(stream_key), name="youtube-go-live"))
    return True


async def _terminal_stream(stream_key: str) -> None:
    """The way that always worked: OBS closed and reopened with --startstreaming
    (the stream key is in its profile), then YouTube gets its Go live."""
    await close_obs()
    prepare_obs_config(stream_key)  # OBS may have saved other settings when it closed
    if launch_obs(streaming=True):
        await asyncio.sleep(20)
        logger.info("OBS: reopened with streaming on")
    STREAM_LIVE.set()
    _keep_task(asyncio.create_task(ensure_youtube_live(stream_key), name="youtube-go-live"))


_TASKS: set = set()


def _keep_task(task: asyncio.Task) -> None:
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def ensure_youtube_live(stream_key: str, timeout: float = 300.0) -> bool:
    """OBS is sending, but YouTube can hold the stream in "Preparing" until
    someone clicks Go live (often after a few quick restarts). This does the
    click: the waiting broadcast on our stream key is moved to live, and if
    there is none, a new one is made, bound to the key and started.

    YOUTUBE_AUTO_GO_LIVE=0 turns it off. Never raises.
    """
    if os.environ.get("YOUTUBE_AUTO_GO_LIVE", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    if not stream_key:
        return False
    try:
        from .settings import PublishSettings
        from .youtube import YouTubeClient

        s = PublishSettings.from_env(read_dotenv=False)
        if not s.youtube_ready:
            return False
        client = YouTubeClient(s.youtube_client_id, s.youtube_client_secret, s.youtube_refresh_token)
    except Exception as exc:
        logger.debug(f"YouTube go live check unavailable: {exc}")
        return False

    async def call(fn, *args):
        return await asyncio.to_thread(fn, *args)

    end = time.time() + timeout
    tried: set[str] = set()
    created = False
    await asyncio.sleep(25)  # the normal auto start usually happens by now
    while time.time() < end:
        try:
            if await call(client.find_active_broadcast_video_id):
                logger.info("YouTube: the stream is live")
                return True
            stream = await call(client.stream_for_key, stream_key)
            if not stream.get("id"):
                logger.warning("YouTube: no stream found for the stream key, cannot press Go live")
                return False
            if stream.get("status") != "active":
                logger.info(f"YouTube: waiting for video to arrive (stream {stream.get('status') or 'unknown'})")
            else:
                waiting = [b for b in await call(client.upcoming_broadcasts) if b["stream"] == stream["id"]]
                fresh = [b for b in waiting if b["id"] not in tried]
                if fresh:
                    broadcast = fresh[0]
                    tried.add(broadcast["id"])
                    logger.warning("YouTube: the stream is waiting for Go live, starting it")
                    if broadcast["monitor"] and broadcast["life"] in ("ready", "created"):
                        try:
                            await call(client.transition, broadcast["id"], "testing")
                            await asyncio.sleep(12)
                        except Exception as exc:
                            logger.debug(f"YouTube: testing step skipped: {str(exc)[:160]}")
                    try:
                        await call(client.transition, broadcast["id"], "live")
                        logger.info("YouTube: Go live sent")
                    except Exception as exc:
                        logger.warning(f"YouTube: Go live refused: {str(exc)[:200]}")
                elif not waiting and not created:
                    created = True
                    logger.warning("YouTube: no broadcast is waiting on the stream key, making a new one")
                    broadcast_id = await call(client.create_broadcast, "VR Agent LIVE 🔴")
                    if broadcast_id:
                        await call(client.bind, broadcast_id, stream["id"])
                        logger.info("YouTube: new broadcast bound to the stream key, it starts by itself")
        except Exception as exc:
            logger.warning(f"YouTube go live check failed: {str(exc)[:200]}")
        await asyncio.sleep(20)
    logger.error("YouTube: still not live after 5 minutes. Open YouTube Studio and press Go live.")
    return False


YOUTUBE_RTMPS = "rtmps://a.rtmps.youtube.com:443/live2"


async def _really_streaming(obs: "OBS", seconds: float = 15) -> bool:
    """True once OBS is connected and the bytes sent keep growing."""
    end = time.time() + seconds
    last = -1
    while time.time() < end:
        await asyncio.sleep(2.5)
        try:
            status = await obs.call("GetStreamStatus")
        except Exception:
            continue
        sent = int(status.get("outputBytes") or 0)
        if status.get("outputActive") and not status.get("outputReconnecting") and last >= 0 and sent > last:
            return True
        last = sent if status.get("outputActive") else -1
    return False


async def stop_and_close() -> None:
    """Stop streaming, then close OBS (only when VR_START_OBS manages it)."""
    try:
        obs = await _connect_when_ready(6)
        if obs is None:
            raise OBSError("no remote control")
        try:
            status = await obs.call("GetStreamStatus")
            if status.get("outputActive"):
                await obs.call("StopStream")
                logger.info("OBS: streaming stopped")
                for _ in range(20):
                    await asyncio.sleep(0.5)
                    if not (await obs.call("GetStreamStatus")).get("outputActive"):
                        break
        finally:
            await obs.__aexit__()
    except Exception as exc:
        logger.warning(f"OBS: could not stop streaming: {exc}")
    if manage_obs():
        try:
            await close_obs()
        except Exception as exc:
            logger.debug(f"OBS: close failed: {exc}")
