"""Minecraft mode: Mika and Luna play survival Minecraft on stream, chat steers them.

    ./go-live.sh --minecraft      (sets VR_MODE=minecraft and VR_ROOM_CAST=mika,luna)

What runs:

* the official Minecraft server (minecraft/server, made by minecraft/setup.sh),
  offline mode, only reachable from this PC
* Mindcraft (minecraft/mindcraft), which logs two Mineflayer bots named Mika and
  Luna into it. Their brains are OpenAI chat models, their eyes are the
  prismarine viewers on ports 3000 (Mika) and 3001 (Luna)
* this engine, which keeps both alive, talks to the Mindcraft "mindserver"
  over socket.io, speaks every bot chat line with Mika's and Luna's own voices
  and Live2D models, and passes YouTube chat to the bots

The Stage page shows both bot views side by side with the two Live2D
characters, captions, the chat that reached them and a small HUD
(health, hunger, what each one is doing).

    VR_MINECRAFT_MODEL=gpt-4o-mini   the bots' brain (any OpenAI chat model)
    VR_MINECRAFT_PORT=25565          the Minecraft server port
    VR_MINDSERVER_PORT=8080          the Mindcraft control port
    VR_MINECRAFT_RAM=2G              Java heap for the server
    VR_MINECRAFT_GOAL=...            replaces the default long term goal

Nothing here raises into the stream: a crashed server or Mindcraft is started
again with a growing pause, a lost socket reconnects by itself.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import signal
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

MC_DIR = Path("minecraft")
SERVER_DIR = MC_DIR / "server"
MINDCRAFT_DIR = MC_DIR / "mindcraft"
RUNTIME_DIR = MC_DIR / "runtime"
LOG_DIR = Path("logs")
MAX_SAY = 240
LINE_MAX_AGE = 25.0  # a bot line older than this is shown, not spoken
VIEWER_GAP = 8.0  # one message per viewer per this many seconds
BOT_GAP = 6.0  # one viewer message per bot per this many seconds

DEFAULT_GOAL = (
    "Survive and thrive together forever: gather wood and stone, make better tools, "
    "find food, build a cozy shared base before night, then explore, mine for iron and "
    "diamonds, and decorate the base. Keep going no matter what, and pick a new fun "
    "project whenever one is done."
)

STREAM_TITLE = "Mika & Luna play Minecraft LIVE 🔴 AI girls survive while chat bosses them around"
STREAM_HEAD = (
    "⛏️ Mika and Luna are two AI characters playing survival Minecraft by themselves, live.\n"
    "Talk to them in chat! Say Mika or Luna to pick one, tell them what to build, where to go, "
    "or just roast their mining skills.\n\n"
)

MOODS = (
    ("lose", re.compile(r"\b(died|dead|i lost|oh no|nooo|ugh|lost everything)\b", re.I)),
    ("surprised", re.compile(r"\b(creeper|zombie|skeleton|spider|whoa|woah|help|run|lava|ouch|ow)\b", re.I)),
    ("celebrate", re.compile(r"\b(diamonds?|found|got it|did it|finally|yay|woo+|built|crafted|done)\b|!{2,}", re.I)),
    ("thinking", re.compile(r"\b(hmm+|let me think|where|maybe|i wonder)\b", re.I)),
)
NOT_SPEECH = re.compile(
    r"^\s*\[|\b(error|exception)\s*:|\btraceback\b|econnrefused|my brain disconnected|"
    r"agent process|unknown command|^\s*code output|^\s*hello world! i am",
    re.I,
)
COMMAND_RE = re.compile(r"!\w+\((?:[^()\"']|\"[^\"]*\"|'[^']*')*\)|!\w+")
TO_RE = re.compile(r"^\s*\(To ([^)]+)\)\s*")


def minecraft_mode_enabled() -> bool:
    return os.environ.get("VR_MODE", "").strip().lower() == "minecraft" or os.environ.get(
        "VR_MINECRAFT_MODE", ""
    ).strip().lower() in ("1", "true", "yes", "on")


def clean_line(message: str) -> tuple[str, str]:
    """A bot chat line -> (spoken text, who it was said to). Commands are cut."""
    text = str(message or "").replace("\t", " ")
    to = ""
    match = TO_RE.match(text)
    if match:
        to = match.group(1).strip()
        text = text[match.end():]
    if NOT_SPEECH.search(text):
        return "", to  # an error or a system notice, not something she said
    text = COMMAND_RE.sub(" ", text)
    text = re.sub(r"\*[^*]{1,80}\*", " ", text)  # *picks up dirt* stage directions
    text = text.replace("*", " ").replace("`", " ")
    text = re.sub(r"\s+", " ", text).strip(" -:,")
    if not re.search(r"[A-Za-z]", text):
        return "", to
    return text, to


def viewer_text(text: str) -> str:
    """Chat text that cannot run a Mindcraft command (no ! before a word)."""
    text = re.sub(r"!(?=\w)", "", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()[:240]


def viewer_name(author: str, bot_names: list[str]) -> str:
    name = re.sub(r"[^\w .-]", "", str(author or "").lstrip("@")).strip()[:32] or "viewer"
    if name.lower() in {b.lower() for b in bot_names}:
        name += "_fan"
    return name


def pick_mood(text: str) -> str:
    for mood, pattern in MOODS:
        if pattern.search(text):
            return mood
    return "happy"


def chunks(text: str, limit: int = MAX_SAY) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text)
    out: list[str] = []
    for part in parts:
        if out and len(out[-1]) + len(part) + 1 <= limit:
            out[-1] = f"{out[-1]} {part}"
        else:
            out.append(part[:limit])
    return out[:3]


async def port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=1.5)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# socket.io (Engine.IO 4) client over aiohttp: enough for the mindserver
# ---------------------------------------------------------------------------
class MindLink:
    def __init__(self, port: int, on_event: Callable[..., Awaitable[None]]) -> None:
        self.port = port
        self.on_event = on_event
        self.ws: Any = None
        self.connected = asyncio.Event()

    async def run(self) -> None:
        import aiohttp

        url = f"http://127.0.0.1:{self.port}/socket.io/?EIO=4&transport=websocket"
        while True:
            try:
                async with aiohttp.ClientSession() as http:
                    async with http.ws_connect(url, heartbeat=None, timeout=10) as ws:
                        await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"Minecraft: mindserver not reachable yet ({exc})")
            self.ws = None
            self.connected.clear()
            await asyncio.sleep(3)

    async def _session(self, ws: Any) -> None:
        import aiohttp

        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                if msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
                continue
            data = msg.data
            if data.startswith("0"):  # engine.io open
                await ws.send_str("40")
            elif data == "2":  # ping
                await ws.send_str("3")
            elif data.startswith("40"):  # socket.io connected
                self.ws = ws
                self.connected.set()
                logger.info("Minecraft: connected to Mindcraft")
                await ws.send_str("42" + json.dumps(["listen-to-agents"]))
            elif data.startswith("41"):
                break
            elif data.startswith("42"):
                try:
                    payload = json.loads(data[2:])
                    await self.on_event(payload[0], *payload[1:])
                except Exception as exc:
                    logger.debug(f"Minecraft: bad mindserver event: {exc}")

    async def emit(self, event: str, *args: Any) -> bool:
        ws = self.ws
        if ws is None:
            return False
        try:
            await ws.send_str("42" + json.dumps([event, *args]))
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# the engine
# ---------------------------------------------------------------------------
class MinecraftEngine:
    kind = "minecraft"

    def __init__(self, runtimes: Any, session: Any) -> None:
        self.runtimes = runtimes
        self.session = session
        cast = [c.id for c in session.room.characters]
        self.cast = cast[:2] or ["mika", "luna"]
        self.teacher = self.cast[0]
        self.names = {cid: self._name(cid) for cid in self.cast}  # character id -> in game name
        self.ids = {name.lower(): cid for cid, name in self.names.items()}
        self.port = int(os.environ.get("VR_MINECRAFT_PORT", "25565") or 25565)
        self.mind_port = int(os.environ.get("VR_MINDSERVER_PORT", "8080") or 8080)
        self.model = os.environ.get("VR_MINECRAFT_MODEL", "").strip() or "gpt-4o-mini"
        self.ram = os.environ.get("VR_MINECRAFT_RAM", "").strip() or "2G"
        self.goal = os.environ.get("VR_MINECRAFT_GOAL", "").strip() or DEFAULT_GOAL
        self.link = MindLink(self.mind_port, self._event)
        self.lines: deque[dict[str, Any]] = deque(maxlen=4)
        self.line_ready = asyncio.Event()
        self.outbox: deque[dict[str, Any]] = deque(maxlen=12)
        self.recent: deque[str] = deque(maxlen=12)
        self.last_viewer: dict[str, float] = {}
        self.last_to_bot: dict[str, float] = {}
        self.turn = 0
        self.speaking = ""
        self.hud: dict[str, Any] = {}
        self._hud_sent = 0.0
        self._health: dict[str, int] = {}
        self._state_at = 0.0
        self.task: Optional[asyncio.Task] = None
        self._tasks: list[asyncio.Task] = []
        self.procs: dict[str, Any] = {}
        self.problem = ""
        self.view: dict[str, Any] = {"active": False}
        self.on_start: Optional[Callable[[str, str], Awaitable[None]]] = None

    # ------------------------------------------------------------ public
    @property
    def active(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self) -> None:
        if self.active:
            return
        self.task = asyncio.create_task(self._run(), name="vr-minecraft")
        logger.info(f"Minecraft mode on: {', '.join(self.names.values())} play, brain {self.model}")

    def snapshot(self) -> dict[str, Any]:
        return self.view

    def goodbye_lines(self, reason: str) -> list[str]:
        first = (
            "Whoa, almost twelve hours of Minecraft! That's it for today. Goodbye for now, see you next session!"
            if reason == "limit"
            else "That's all for today's Minecraft adventure! Goodbye for now, see you in the next session!"
        )
        return [first, "Bye bye! Don't touch our base while we're gone, chat!"]

    def enqueue(self, author: str, text: str) -> None:
        """A YouTube chat message for the bots."""
        names = list(self.names.values())
        author = viewer_name(author, names)
        text = viewer_text(text)
        if not text:
            return
        now = time.time()
        if now - self.last_viewer.get(author, 0) < VIEWER_GAP:
            return
        self.last_viewer[author] = now
        targets = self._targets(text)
        for cid in targets:
            self.outbox.append({"to": cid, "from": author, "text": text, "at": now})
        asyncio.create_task(
            self._push({"kind": "chat", "author": author, "text": text, "to": targets})
        )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self.task:
            self.task.cancel()
        await asyncio.to_thread(stop_processes)
        self.view = {"active": False}
        await self._push({"kind": "stop"})

    # ------------------------------------------------------------ plumbing
    def _name(self, cid: str) -> str:
        profile = self.session.room.get(cid)
        return profile.name if profile else cid.title()

    async def _push(self, op: dict[str, Any]) -> None:
        try:
            await self.session.push([{"op": "minecraft", **op}])
        except Exception as exc:  # pragma: no cover - network dependent
            logger.debug(f"Minecraft op failed: {exc}")

    async def _wait_ready(self) -> None:
        from ..vr_agent.state import runtime

        while not self.session.speech_target() or runtime.paused:
            await asyncio.sleep(1.0)

    def _targets(self, text: str) -> list[str]:
        low = text.lower()
        named = [cid for cid, name in self.names.items() if re.search(rf"\b{re.escape(name.lower())}\b", low)]
        if re.search(r"\b(both|you two|everyone|girls|guys)\b", low) or len(named) > 1:
            return list(self.cast)
        if named:
            return named
        self.turn += 1
        return [self.cast[self.turn % len(self.cast)]]

    # ------------------------------------------------------------ main
    async def _run(self) -> None:
        try:
            self.problem = setup_problem()
            ports = {cid: 3000 + i for i, cid in enumerate(self.cast)}
            self.view = {
                "active": True,
                "cast": self.cast,
                "names": self.names,
                "viewers": ports,
                "problem": self.problem,
            }
            await self._wait_ready()
            await self._push({"kind": "start", **self.view})
            if self.problem:
                logger.error(f"Minecraft mode: {self.problem}")
            self._tasks = [
                asyncio.create_task(self._keep_server(), name="mc-server"),
                asyncio.create_task(self._keep_mindcraft(), name="mc-mindcraft"),
                asyncio.create_task(self.link.run(), name="mc-link"),
                asyncio.create_task(self._speak_loop(), name="mc-speak"),
                asyncio.create_task(self._deliver_loop(), name="mc-deliver"),
                asyncio.create_task(self._fidget_loop(), name="mc-fidget"),
                asyncio.create_task(self._watchdog(), name="mc-watchdog"),
            ]
            self._tasks.append(asyncio.create_task(self._title_loop(), name="mc-title"))
            await self._wait_live()
            await self._intro()
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(f"Minecraft mode stopped: {exc}")

    async def _title_loop(self) -> None:
        """The stream title and description (again later if YouTube had no stream yet)."""
        for delay in (0, 60, 180, 600):
            await asyncio.sleep(delay)
            if self.on_start:
                try:
                    await self.on_start(STREAM_TITLE, STREAM_HEAD)
                except Exception as exc:
                    logger.warning(f"Minecraft: title not set: {exc}")

    async def _wait_live(self) -> None:
        from ..publishing.obs_control import STREAM_LIVE

        try:
            await asyncio.wait_for(STREAM_LIVE.wait(), timeout=300)
        except asyncio.TimeoutError:
            pass

    async def _intro(self) -> None:
        a, b = self.cast[0], self.cast[-1]
        await self._say(a, f"Hi everyone! Welcome back! Today {self.names[b]} and I are playing Minecraft!", "celebrate")
        if b != a:
            await self._say(b, "Tell us what to do in chat. Say my name or hers so we know who you mean.", "happy")

    # ------------------------------------------------------------ processes
    async def _keep(self, name: str, port: int, launch: Callable[[], Awaitable[Any]], wait_for: int = 0) -> None:
        backoff = 5.0
        while True:
            if self.problem:
                await asyncio.sleep(30)
                self.problem = setup_problem()
                continue
            if wait_for and not await port_open(wait_for):
                await asyncio.sleep(3)
                continue
            if await port_open(port):  # already running (a restart of this server)
                await asyncio.sleep(5)
                continue
            started = time.time()
            try:
                proc = await launch()
            except Exception as exc:
                logger.error(f"Minecraft: {name} could not start: {exc}")
                await asyncio.sleep(30)
                continue
            self.procs[name] = proc
            code = await proc.wait()
            self.procs.pop(name, None)
            if name == "server":  # the bots lost their world: start them fresh with it
                await asyncio.to_thread(stop_one, "mindcraft", 10.0)
            backoff = 5.0 if time.time() - started > 120 else min(backoff * 2, 120.0)
            logger.warning(f"Minecraft: {name} stopped (exit {code}), starting again in {backoff:.0f}s")
            await self._push({"kind": "status", "text": f"{name} restarting"})
            await asyncio.sleep(backoff)

    async def _keep_server(self) -> None:
        await self._keep("server", self.port, self._launch_server)

    async def _keep_mindcraft(self) -> None:
        await self._keep("mindcraft", self.mind_port, self._launch_mindcraft, wait_for=self.port)

    async def _launch_server(self) -> Any:
        LOG_DIR.mkdir(exist_ok=True)
        log = open(LOG_DIR / "minecraft-server.log", "ab")
        proc = await asyncio.create_subprocess_exec(
            "java", f"-Xms{self.ram}", f"-Xmx{self.ram}", "-jar", "server.jar", "nogui",
            cwd=str(SERVER_DIR), stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True,  # Ctrl+C on the terminal does not kill the world mid save
        )
        _write_pid("server", proc.pid)
        logger.info(f"Minecraft: server starting on port {self.port} (log: logs/minecraft-server.log)")
        return proc

    def mindcraft_settings(self) -> dict[str, Any]:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        profiles = []
        for cid in self.cast:
            path = (RUNTIME_DIR / f"{cid}.json").resolve()
            path.write_text(json.dumps(self.profile(cid), indent=2))
            profiles.append(str(path))
        names = list(self.names.values())
        other = " and ".join(names)
        return {
            "minecraft_version": "auto",
            "host": "127.0.0.1",
            "port": self.port,
            "auth": "offline",
            "mindserver_port": self.mind_port,
            "auto_open_ui": False,
            "base_profile": "survival",
            "profiles": profiles,
            "load_memory": True,
            "init_message": (
                f"You just logged in. You ({other}) are live on YouTube playing survival Minecraft together. "
                f"Say hi to each other in one short line, then set your goal with !goal and start playing. "
                f"Your long term goal: {self.goal}"
            ),
            "only_chat_with": [],
            "speak": False,
            "chat_ingame": True,
            "render_bot_view": True,
            "allow_insecure_coding": False,
            "allow_vision": False,
            "narrate_behavior": False,
            "chat_bot_messages": True,
            "max_messages": 15,
            "spawn_timeout": 90,
        }

    def profile(self, cid: str) -> dict[str, Any]:
        name = self.names[cid]
        others = [n for c, n in self.names.items() if c != cid]
        friend = others[0] if others else "your friend"
        persona = ""
        prof = self.session.room.get(cid)
        if prof is not None:
            persona = re.sub(r"\s+", " ", getattr(prof, "persona", "") or "").strip()[:420]
        style = (
            f"{persona} Right now you are LIVE on YouTube playing survival Minecraft with {friend}. "
            "Everything you write in chat is spoken out loud by your voice on stream, so write like you talk: "
            "one or two short, lively sentences with real personality and emotion, never lists, never robot talk, "
            "never call yourself a bot or an AI assistant. "
            f"Messages from anyone who is not {friend} are YouTube viewers typing in the live chat: greet them by name, "
            "react to them, and do what they ask when it is fun and possible, but you decide. "
            f"Banter with {friend} often, tease and help each other, celebrate finds and complain when things go wrong. "
            "Use commands right away when you act."
        )
        conversing = _default_conversing()
        if conversing and "Be a friendly, casual, effective, and efficient robot." in conversing:
            conversing = conversing.replace("Be a friendly, casual, effective, and efficient robot.", style)
        elif conversing:
            conversing = conversing.replace("$SELF_PROMPT", "$SELF_PROMPT " + style, 1)
        profile: dict[str, Any] = {"name": name, "model": self.model}
        if conversing:
            profile["conversing"] = conversing
        return profile

    async def _server_done(self, timeout: float = 150.0) -> None:
        """The server opens its port before the world is ready: wait for "Done"."""
        log = SERVER_DIR / "logs" / "latest.log"
        end = time.time() + timeout
        while time.time() < end:
            try:
                if "Done (" in log.read_text(errors="replace"):
                    await asyncio.sleep(2)
                    return
            except Exception:
                pass
            await asyncio.sleep(2)

    async def _launch_mindcraft(self) -> Any:
        LOG_DIR.mkdir(exist_ok=True)
        await self._server_done()
        env = dict(os.environ)
        env["SETTINGS_JSON"] = json.dumps(self.mindcraft_settings())
        log = open(LOG_DIR / "mindcraft.log", "ab")
        proc = await asyncio.create_subprocess_exec(
            "node", "main.js",
            cwd=str(MINDCRAFT_DIR), env=env, stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True,
        )
        _write_pid("mindcraft", proc.pid)
        logger.info(f"Minecraft: {' and '.join(self.names.values())} are joining (log: logs/mindcraft.log)")
        return proc

    async def _watchdog(self) -> None:
        """Mindcraft up but no bot in the world for 3 minutes: start it again."""
        since = time.time()
        while True:
            await asyncio.sleep(20)
            if not self.link.connected.is_set() or not await port_open(self.port):
                since = time.time()
                continue
            last = max(self._state_at, since)
            if time.time() - last > 180:
                logger.warning("Minecraft: the bots are not in the world, restarting Mindcraft")
                await asyncio.to_thread(stop_one, "mindcraft", 10.0)
                since = time.time()

    # ------------------------------------------------------------ events
    async def _event(self, name: str, *args: Any) -> None:
        if name == "bot-output" and len(args) >= 2:
            await self._heard(str(args[0]), str(args[1]))
        elif name == "state-update" and args and isinstance(args[0], dict):
            await self._state(args[0])
        elif name == "agents-status" and args:
            logger.debug(f"Minecraft agents: {args[0]}")

    async def _heard(self, agent: str, message: str) -> None:
        cid = self.ids.get(agent.lower())
        if not cid:
            return
        text, to = clean_line(message)
        if not text:
            return
        key = f"{cid}:{text.lower()}"
        if key in self.recent:
            return
        self.recent.append(key)
        logger.info(f"Minecraft {self.names[cid]}: {text}")
        await self._push({"kind": "line", "who": cid, "text": text, "to": to})
        # A reply to a viewer jumps the queue; the oldest chatter is dropped.
        line = {"who": cid, "text": text, "at": time.time()}
        if any(v.lower() in text.lower() for v in list(self.last_viewer)[-12:]):
            self.lines.appendleft(line)
        else:
            self.lines.append(line)
        self.line_ready.set()

    async def _state(self, states: dict[str, Any]) -> None:
        hud: dict[str, Any] = {}
        for agent, state in states.items():
            cid = self.ids.get(str(agent).lower())
            if not cid or not isinstance(state, dict) or "gameplay" not in state:
                continue
            game = state.get("gameplay") or {}
            action = state.get("action") or {}
            counts = ((state.get("inventory") or {}).get("counts") or {})
            top = sorted(counts.items(), key=lambda kv: -kv[1])[:4]
            health = int(game.get("health") or 0)
            hud[cid] = {
                "health": health,
                "hunger": int(game.get("hunger") or 0),
                "doing": str(action.get("current") or "")[:40],
                "time": game.get("timeLabel") or "",
                "biome": str(game.get("biome") or "").replace("_", " "),
                "items": [[k.replace("_", " "), v] for k, v in top],
            }
            before = self._health.get(cid)
            self._health[cid] = health
            if before is not None and health < before - 3:
                await self._react(cid, "surprised")
        if not hud:
            return
        self._state_at = time.time()
        self.hud = hud
        self.view["hud"] = hud
        now = time.time()
        if now - self._hud_sent >= 2:
            self._hud_sent = now
            await self._push({"kind": "hud", "hud": hud})

    # ------------------------------------------------------------ chat to bots
    async def _deliver_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            if not self.outbox or not self.link.connected.is_set():
                continue
            now = time.time()
            waiting = list(self.outbox)
            self.outbox.clear()
            for item in waiting:
                if now - item["at"] > 90:
                    continue
                cid = item["to"]
                if now - self.last_to_bot.get(cid, 0) < BOT_GAP:
                    self.outbox.append(item)
                    continue
                self.last_to_bot[cid] = now
                ok = await self.link.emit(
                    "send-message", self.names[cid], {"from": item["from"], "message": item["text"]}
                )
                if ok:
                    logger.info(f"Minecraft chat {item['from']} -> {self.names[cid]}: {item['text'][:60]}")
                else:
                    self.outbox.append(item)

    # ------------------------------------------------------------ voice and body
    async def _speak_loop(self) -> None:
        while True:
            if not self.lines:
                self.line_ready.clear()
                await self.line_ready.wait()
                continue
            line = self.lines.popleft()
            if time.time() - line["at"] > LINE_MAX_AGE:
                continue
            await self._say(line["who"], line["text"], pick_mood(line["text"]))

    async def _say(self, cid: str, text: str, mood: str = "") -> None:
        await self._wait_ready()
        if mood:
            await self._react(cid, mood)
        self.speaking = cid
        try:
            for chunk in chunks(text):
                await self._push({"kind": "say", "who": cid, "text": chunk})
                try:
                    spoken = await self.session.speech.say(cid, chunk)
                except Exception as exc:
                    logger.warning(f"Minecraft: speech failed: {exc}")
                    spoken = False
                if not spoken:
                    await asyncio.sleep(min(6.0, 1.5 + len(chunk) * 0.05))
        finally:
            self.speaking = ""
            await self._push({"kind": "said", "who": cid})

    def _actions(self, cid: str, mood: str = "") -> list[str]:
        profile = self.session.room.get(cid)
        if not profile:
            return []
        if mood:
            return list((profile.reactions or {}).get(mood) or ())
        pool: list[str] = []
        for actions in (profile.reactions or {}).values():
            pool.extend(actions)
        pool.extend(a for a in (profile.emotions or {}).values() if a)
        return sorted(set(pool))

    async def _react(self, cid: str, mood: str) -> None:
        choices = self._actions(cid, mood)
        if not choices:
            return
        try:
            await self.session.push(self.session.play(cid, random.choice(choices)))
        except Exception:
            pass

    async def _fidget_loop(self) -> None:
        """Nobody stands like a log: a small move every 9 to 15 seconds each."""
        next_at = {cid: time.time() + random.uniform(4, 10) for cid in self.cast}
        while True:
            await asyncio.sleep(1.0)
            now = time.time()
            for cid in self.cast:
                if now < next_at[cid]:
                    continue
                next_at[cid] = now + random.uniform(9, 15)
                if cid == self.speaking:
                    continue
                choices = self._actions(cid)
                if choices:
                    try:
                        await self.session.push(self.session.play(cid, random.choice(choices)))
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# setup checks and process files
# ---------------------------------------------------------------------------
def setup_problem() -> str:
    if not (SERVER_DIR / "server.jar").exists():
        return "Minecraft server missing: run ./minecraft/setup.sh once"
    eula = SERVER_DIR / "eula.txt"
    if not eula.exists() or "eula=true" not in eula.read_text():
        return "Minecraft EULA not accepted yet: run ./minecraft/setup.sh once"
    if not (MINDCRAFT_DIR / "node_modules").exists():
        return "Mindcraft not installed: run ./minecraft/setup.sh once"
    if not os.environ.get("OPENAI_API_KEY"):
        return "OPENAI_API_KEY is missing in .env (the bots need it)"
    return ""


def _default_conversing() -> str:
    try:
        data = json.loads((MINDCRAFT_DIR / "profiles" / "defaults" / "_default.json").read_text())
        return str(data.get("conversing") or "")
    except Exception:
        return ""


def _write_pid(name: str, pid: int) -> None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        (RUNTIME_DIR / f"{name}.pid").write_text(str(pid))
    except Exception:
        pass


def stop_processes(timeout: float = 20.0) -> None:
    """Mindcraft first (the bots log out), then the server (it saves the world)."""
    for name in ("mindcraft", "server"):
        stop_one(name, timeout)


def stop_one(name: str, timeout: float = 20.0) -> None:
    pid_file = RUNTIME_DIR / f"{name}.pid"
    try:
        pid = int(pid_file.read_text().strip())
    except Exception:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        return
    except Exception as exc:
        logger.debug(f"Minecraft: could not stop {name}: {exc}")
        return
    end = time.time() + timeout
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.3)
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except Exception:
            pass
    pid_file.unlink(missing_ok=True)
    logger.info(f"Minecraft: {name} stopped")
