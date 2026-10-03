"""Minecraft mode: Mika and Luna play survival Minecraft on stream, chat steers them.

    ./go-live.sh --minecraft      (sets VR_MODE=minecraft and VR_ROOM_CAST=mika,luna)

What runs:

* the official Minecraft server (minecraft/server, made by minecraft/setup.sh),
  offline mode, only reachable from this PC
* Mindcraft (minecraft/mindcraft), which logs two Mineflayer bots named Mika and
  Luna into it. Their brains are OpenAI chat models, their eyes are the
  prismarine viewers on ports 3790 (Mika) and 3791 (Luna); 3000 is left
  alone because so many other tools use it
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
    VR_MINECRAFT_DIFFICULTY=peaceful no hostile mobs (easy and up: the bots
                                     fight at night and get kicked)
    VR_MINECRAFT_GOAL=...            replaces the default long term goal
    VR_MINECRAFT_THINK_SECONDS=10    at most one AI call per bot this often:
                                     lower is livelier but costs more
    VR_MINECRAFT_CREATIVE=1          creative mode (default): every block, they fly,
                                     and they build each project piece by piece
                                     in front of the camera (0 = survival, the
                                     old gather and the build appears)
    VR_MINECRAFT_PLAYER=YourName     your Minecraft name: when you join the
                                     world with the real game, you become an
                                     invisible spectator that looks through
                                     the eyes of whoever talks, and the Stage
                                     page turns see-through so OBS shows your
                                     game window under it (real graphics, no
                                     black or white web view). Without you in
                                     the world the web views are used.

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

from ..vr_agent.text_safety import strip_emoji

MC_DIR = Path("minecraft")
SERVER_DIR = MC_DIR / "server"
MINDCRAFT_DIR = MC_DIR / "mindcraft"
RUNTIME_DIR = MC_DIR / "runtime"
LOG_DIR = Path("logs")
THUMBNAIL = Path("assets/thumbnails/minecraft.jpg")  # uploaded to each Minecraft stream
MAX_SAY = 240
LINE_MAX_AGE = 25.0  # a bot line older than this is shown, not spoken
VIEWER_GAP = 8.0  # one message per viewer per this many seconds
BOT_GAP = 6.0  # one viewer message per bot per this many seconds
STUCK_SECONDS = 60  # within STILL_BLOCKS this long: she is teleported out and given her goal again
STILL_BLOCKS = 3.0
JUMP_BLOCKS = 16.0  # moved this far between two updates: a teleport or respawn, reload the view
GOAL_RETRY = 30.0  # seconds between forced goal restarts for one bot
VIEWER_LINE_MAX_AGE = 60.0  # an answer to a viewer is spoken even if it waited this long
# Every new project part: a kit for each girl, so they always have materials.
KIT = (
    ("stone", 64), ("oak_planks", 64), ("sand", 32), ("glass", 32), ("torch", 16),
    ("bread", 16), ("stone_pickaxe", 1), ("stone_axe", 1), ("stone_shovel", 1),
)
# Creative: the engine moves them, the AI only talks (and may hand items over).
CREATIVE_BLOCKED = [
    "!collectBlocks", "!searchForBlock", "!searchForEntity", "!moveAway", "!goToCoordinates",
    "!goToPlayer", "!followPlayer", "!goToRememberedPlace", "!rememberHere", "!craftRecipe",
    "!smeltItem", "!clearFurnace", "!placeHere", "!attack", "!goToBed", "!stay", "!goToSurface",
    "!putInChest", "!takeFromChest", "!viewChest", "!discard", "!consume", "!equip", "!useOn",
    "!startConversation", "!endConversation",
]
PIECE_SECONDS = (4.0, 20.0)  # creative: at least / at most this long per piece
CAMERA_HOLD = 15.0  # the camera stays on one girl at least this long
CAMERA_REFRESH = 20.0  # spectate again this often (a respawn ends it)
DIRECTOR = "Director"  # a plain command from this name runs right away in Mindcraft (no AI call)
# Mindcraft's automatic behaviours. elbow_room fought our teleports (the HUD
# showed "mode:elbow_room" for minutes) and idle_staring kept swinging the
# camera around; hunting chased the farm animals and pets.
MODES = {
    "self_preservation": True,
    "unstuck": True,
    "cowardice": False,
    "self_defense": True,
    "hunting": False,
    "item_collecting": True,
    "torch_placing": True,
    "elbow_room": False,
    "idle_staring": False,
    "cheat": False,
}
# Creative: Mindcraft's own behaviours that fight hovering and building.
CREATIVE_MODES = {**MODES, "self_preservation": False, "unstuck": False, "item_collecting": False,
                  "torch_placing": False, "self_defense": False}
TOGETHER_BLOCKS = 10
VIEWER_PORT_BASE = 3790  # the bots' first person views (Mindcraft's default 3000 often clashes)  # farther apart than this for a minute: one walks back

DEFAULT_GOAL = (
    "Survive and thrive together forever: gather wood and stone, make better tools, "
    "find food, build a cozy shared base before night, then explore, mine for iron and "
    "diamonds, and decorate the base. Keep going no matter what, and pick a new fun "
    "project whenever one is done."
)

STREAM_TITLE = "Mika & Luna play Minecraft LIVE 🔴 AI girls survive while chat bosses them around"
CREATIVE_TITLE = "Mika & Luna build a castle and a REAL neural network in Minecraft LIVE 🔴 chat joins in"
CREATIVE_HEAD = (
    "🧠 Mika and Luna are two AI characters building in creative Minecraft by themselves, live: a castle, "
    "then a real neural network (written from scratch, trained live with backpropagation) that reads "
    "handwritten digits on a giant wall.\n"
    "Talk to them in chat! Say Mika or Luna to pick one, or type \"draw 7\" and watch the network guess it.\n\n"
)
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
    r"agent process|unknown command|^\s*code output|^\s*hello world! i am|agent stopped|"
    r"self-prompting|auto-prompts|did not use command|^\s*(sure|ok)?[.!,]?\s*setting (my|the) goal",
    re.I,
)
COMMAND_RE = re.compile(r"!\w+\((?:[^()\"']|\"[^\"]*\"|'[^']*')*\)|!\w+")
TO_RE = re.compile(r"^\s*\(To ([^)]+)\)\s*")
# "draw 7", "predict a 3", "can it guess number 5": the neural network reads that digit
DIGIT_ASK = re.compile(r"\b(?:draw|write|show|predict|guess|read|test|try|number|digit)\b\D{0,14}?\b([0-9])\b", re.I)


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
    text = strip_emoji(text)
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


# Node binds "localhost" to ::1 on many Linux PCs (Mindcraft does), so both
# loopback addresses are tried.
LOOPBACKS = ("127.0.0.1", "::1")


async def port_open(port: int, host: str = "") -> bool:
    for candidate in (host,) if host else LOOPBACKS:
        try:
            _reader, writer = await asyncio.wait_for(asyncio.open_connection(candidate, port), timeout=1.5)
        except Exception:
            continue
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
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

        hosts = ["127.0.0.1", "[::1]"]
        while True:
            host = hosts[0]
            url = f"http://{host}:{self.port}/socket.io/?EIO=4&transport=websocket"
            try:
                async with aiohttp.ClientSession() as http:
                    async with http.ws_connect(url, heartbeat=None, timeout=10) as ws:
                        await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"Minecraft: mindserver not reachable on {host} yet ({exc})")
                hosts.append(hosts.pop(0))  # try the other loopback next
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
        # peaceful: no hostile mobs. With mobs the bots fight at night, and the
        # fight code sends moves the server rejects, so it kicks them out.
        self.difficulty = os.environ.get("VR_MINECRAFT_DIFFICULTY", "").strip().lower() or "peaceful"
        self.goal = os.environ.get("VR_MINECRAFT_GOAL", "").strip() or DEFAULT_GOAL
        self.creative = os.environ.get("VR_MINECRAFT_CREATIVE", "1").strip().lower() not in ("0", "false", "no", "off")
        # Seconds between AI calls per bot: the main cost knob (see the module doc).
        self.cooldown = max(2.0, float(os.environ.get("VR_MINECRAFT_THINK_SECONDS", "10") or 10))
        self.link = MindLink(self.mind_port, self._event)
        self.lines: deque[dict[str, Any]] = deque(maxlen=4)
        self.viewer_lines: deque[dict[str, Any]] = deque(maxlen=6)  # answers to chat, spoken first
        self.said: deque[str] = deque(maxlen=6)  # the last spoken lines, context for answers
        self._kind: dict[str, str] = {}  # Mindcraft activity: acting, thinking, chatting, stopped, idle
        self._goal_at: dict[str, float] = {}
        self._escaped_at: dict[str, float] = {}
        self._goal_step = ""
        self._llm: Any = None
        # The real game as the camera (VR_MINECRAFT_PLAYER).
        self.camera_player = re.sub(r"[^\w]", "", os.environ.get("VR_MINECRAFT_PLAYER", ""))[:16]
        self.camera_on = False
        self.cam_focus = ""
        self._cam_focus_at = 0.0
        self._cam_sent_at = 0.0
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
        self._seen_at: dict[str, float] = {}
        self._spoke_at: dict[str, float] = {}
        self._pos: dict[str, tuple[float, float, float]] = {}
        self._apart_since = 0.0
        self._anchor: dict[str, tuple[float, float, float, float]] = {}  # x, y, z, since
        self._nudged_at = 0.0
        self.task: Optional[asyncio.Task] = None
        self._tasks: list[asyncio.Task] = []
        self.procs: dict[str, Any] = {}
        self.problem = ""
        self.view: dict[str, Any] = {"active": False}
        self.on_start: Optional[Callable[..., Awaitable[None]]] = None
        from .minecraft_projects import ProjectTracker

        self.projects = ProjectTracker(list(self.names.values()), rcon_command, self._push, self._tell_both)
        self.projects.creative = self.creative
        self._builder_turn = 0
        from .minecraft_net_show import NetShow

        self.net_show = NetShow(self.projects, lambda *a, **k: rcon_command(*a, **k), self._tell_one, self._push)

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
        ask = DIGIT_ASK.search(text)
        heard = text  # what the girls get; the screen shows the comment as typed
        if ask and self.net_show.request(int(ask.group(1)), author):
            heard += f" (The neural network will read a {ask.group(1)} on the board in a moment.)"
        # The asked girl answers out loud right away (one short AI call here).
        # Mindcraft alone took minutes: a bot drops its reply whenever another
        # message reaches it while it is still thinking.
        answer = targets[0] if len(targets) == 1 else self._quietest(targets)
        asyncio.create_task(self._quick_reply(answer, author, heard))
        for cid in targets:
            note = (
                " (You already answered out loud. Do not greet again: if this asks for something, "
                "do it now with a command, otherwise keep working on your goal.)"
                if cid == answer
                else ""
            )
            # No emoji reaches the bots: they copy what they read.
            self.outbox.append({"to": cid, "from": author, "text": (strip_emoji(heard) or heard) + note, "at": now})
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

    def _present(self) -> list[str]:
        """Bots seen in the world in the last 30 s (all of them before any state)."""
        now = time.time()
        here = [c for c in self.cast if now - self._seen_at.get(c, 0) < 30]
        return here or list(self.cast)

    def _targets(self, text: str) -> list[str]:
        targets = self._pick_targets(text)
        here = self._present()
        # A message for someone who is not in the world goes to whoever is.
        return [c for c in targets if c in here] or here[:1]

    def _pick_targets(self, text: str) -> list[str]:
        low = text.lower()
        named = [cid for cid, name in self.names.items() if re.search(rf"\b{re.escape(name.lower())}\b", low)]
        if re.search(r"\b(both|you two|everyone|girls|guys)\b", low) or len(named) > 1:
            return list(self.cast)
        if named:
            return named
        here = self._present()
        return [self._quietest(here)]

    def _quietest(self, cids: list[str]) -> str:
        """Whoever spoke least recently, so both girls get turns."""
        self.turn += 1
        order = sorted(cids, key=lambda c: (self._spoke_at.get(c, 0), (self.cast.index(c) + self.turn) % 2))
        return order[0]

    # ------------------------------------------------------------ main
    async def _run(self) -> None:
        try:
            self.problem = setup_problem()
            ports = {cid: VIEWER_PORT_BASE + i for i, cid in enumerate(self.cast)}
            self.view = {
                "active": True,
                "cast": self.cast,
                "names": self.names,
                "viewers": ports,
                "camera": "web",
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
                asyncio.create_task(self._camera_loop(), name="mc-camera"),
                asyncio.create_task(self._net_loop(), name="mc-net"),
                asyncio.create_task(self._build_loop(), name="mc-build"),
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
                    title, head = (CREATIVE_TITLE, CREATIVE_HEAD) if self.creative else (STREAM_TITLE, STREAM_HEAD)
                    await self.on_start(title, head, str(THUMBNAIL) if THUMBNAIL.exists() else "")
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
            await asyncio.sleep(backoff)

    async def _keep_server(self) -> None:
        await self._keep("server", self.port, self._launch_server)

    async def _keep_mindcraft(self) -> None:
        await self._keep("mindcraft", self.mind_port, self._launch_mindcraft, wait_for=self.port)

    async def _launch_server(self) -> Any:
        LOG_DIR.mkdir(exist_ok=True)
        set_difficulty(self.difficulty)
        enable_rcon()
        _set_properties({
            "gamemode": "creative" if self.creative else "survival",
            "force-gamemode": "true",  # everyone who joins gets it (the camera becomes a spectator after)
            "allow-flight": "true",
        })
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
            "base_profile": "creative" if self.creative else "survival",
            "profiles": profiles,
            "load_memory": True,
            "init_message": (
                f"You just logged in. You ({other}) are live on YouTube in creative Minecraft, building big projects "
                "from scratch together. Say hi to your friend and to chat in one short line."
            ) if self.creative else (
                f"You just logged in. You ({other}) are live on YouTube playing survival Minecraft as a team. "
                f"You are standing next to each other. Say hi to your friend in one short line, then get to work. "
                f"Stay close to each other the whole time, split the work (one gathers wood, the other stone and food), "
                f"share items with !givePlayer and build ONE shared base together. Your long term goal: {self.goal}"
            ),
            "only_chat_with": [],
            "speak": False,
            "chat_ingame": True,
            "render_bot_view": True,
            "allow_insecure_coding": False,
            "allow_vision": False,
            "narrate_behavior": False,
            "chat_bot_messages": True,
            "max_messages": 12,
            "blocked_actions": BLOCKED_ACTIONS + (CREATIVE_BLOCKED if self.creative else []),
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
        if cid == self.cast[0]:
            role = (
                f"In the team you are the one with bold, chaotic ideas (a castle in the sky! fight that zombie!) "
                f"and you hate being told no by {friend}. "
            )
        else:
            role = (
                f"In the team you are the practical one: question {friend}'s wild ideas, argue for the smarter plan, "
                "and point out what you still need first (tools, food, shelter). "
            )
        style = (
            f"{persona} Right now you are LIVE on YouTube playing survival Minecraft with {friend} in the same world. "
            f"{role}Before starting anything new, argue it out with {friend} in a line or two each, settle it "
            "(or let chat decide), then split the work and do it. "
            "Everything you write in chat is spoken out loud by your voice on stream, so write like you talk: "
            "one or two short, lively sentences with real personality and emotion, never lists, never robot talk, "
            "never call yourself a bot or an AI assistant. "
            "NEVER use emojis, emoticons or symbols like :) or <3, your voice reads them out loud: plain words only. "
            f"Messages from anyone who is not {friend} are YouTube viewers typing in the live chat: greet them by name, "
            "react to them, and do what they ask when it is fun and possible, but you decide. "
            f"You and {friend} are a team: stay near her, "
            "split the work, give her items she needs, and build one shared base. "
            f"Banter with {friend} often, tease and help each other, celebrate finds and complain when things go wrong. "
            "Use commands right away when you act. "
            "Play smart: stay within about 40 blocks of your base and of your friend. When a search finds nothing, "
            "do NOT repeat it and do NOT wander far away: gather something else useful nearby instead. "
            "Never dig straight down. If an action fails twice, change the plan instead of retrying it. "
            "Every reply that is not pure chat must contain a command, so you keep doing something."
        )
        if self.creative:
            style = (
                f"{persona} Right now you are LIVE on YouTube in CREATIVE Minecraft with {friend}: you can fly and have "
                f"every block. Together you build big projects from scratch, piece by piece: a castle, then a REAL neural "
                f"network that learns to read handwritten digits, a farm, a garden. {role}"
                "You fly to each spot and place the blocks yourselves (that happens automatically, you never need "
                "movement or gathering commands). Your job is the talking: say what you are building right now, argue "
                f"with {friend} about how it should look, complain when it is tedious, be proud when a part is done. "
                "About the neural network you really know your stuff: inputs, hidden layers, weights, backpropagation, "
                "loss, accuracy, and you get nervous when it guesses wrong. "
                "Everything you write in chat is spoken out loud by your voice on stream, so write like you talk: "
                "one or two short, lively sentences with real personality and emotion, never lists, never robot talk, "
                "never call yourself a bot or an AI assistant. "
                "NEVER use emojis, emoticons or symbols like :) or <3, your voice reads them out loud: plain words only. "
                f"Messages from anyone who is not {friend} are YouTube viewers typing in the live chat: greet them by "
                "name and react to them. Do not use commands, just talk."
            )
        profile: dict[str, Any] = {
            "name": name,
            "model": self.model,
            "modes": dict(CREATIVE_MODES if self.creative else MODES),
            "cooldown": int(self.cooldown * 1000),  # at most one AI call per bot this often
        }
        conversing = cache_friendly(_default_conversing(), style)
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
        await asyncio.to_thread(patch_mindcraft)
        env = dict(os.environ)
        env["SETTINGS_JSON"] = json.dumps(self.mindcraft_settings())
        # Mindcraft listens on "localhost": make that 127.0.0.1, not ::1.
        env["NODE_OPTIONS"] = (env.get("NODE_OPTIONS", "") + " --dns-result-order=ipv4first").strip()
        env["VR_VIEWER_PORT_BASE"] = str(VIEWER_PORT_BASE)
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
        """One of them out of the world for 2 minutes (Mindcraft gives up on a
        bot that crashes twice quickly): start Mindcraft again."""
        since = time.time()
        while True:
            await asyncio.sleep(20)
            if not self.link.connected.is_set() or not await port_open(self.port):
                since = time.time()
                continue
            now = time.time()
            missing = [c for c in self.cast if now - max(self._seen_at.get(c, 0), since) > 120]
            if missing:
                # Ask the server who is really online before restarting both.
                online = await rcon_online()
                if online is not None:
                    missing = [c for c in missing if self.names[c].lower() not in online]
                    if not missing:
                        since = time.time()
                        continue
            if missing:
                names = " and ".join(self.names[c] for c in missing)
                logger.warning(f"Minecraft: {names} left the world, restarting Mindcraft")
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
        counts_of: dict[str, dict[str, int]] = {}
        for agent, state in states.items():
            cid = self.ids.get(str(agent).lower())
            if not cid or not isinstance(state, dict) or "gameplay" not in state:
                continue
            self._seen_at[cid] = time.time()
            game = state.get("gameplay") or {}
            action = state.get("action") or {}
            counts = ((state.get("inventory") or {}).get("counts") or {})
            counts_of[cid] = {str(k): int(v) for k, v in counts.items() if isinstance(v, (int, float))}
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
            self._kind[cid] = str(action.get("kind") or "")
            pos = game.get("position") or {}
            if isinstance(pos, dict) and "x" in pos:
                new = (float(pos["x"]), float(pos.get("y", 0)), float(pos["z"]))
                old = self._pos.get(cid)
                self._pos[cid] = new
                if old and ((new[0] - old[0]) ** 2 + (new[2] - old[2]) ** 2) ** 0.5 > JUMP_BLOCKS:
                    await self._reload_view(cid)  # a teleport or respawn: the old chunks are gone
                await self._direct(cid)
            before = self._health.get(cid)
            self._health[cid] = health
            if before is not None and health < before - 3:
                await self._react(cid, "surprised")
        if not hud:
            return
        if not self.creative:
            await self._keep_together()
        try:
            await self.projects.update(
                {self.names[c]: self._pos[c] for c in hud if c in self._pos},
                {self.names[c]: counts_of.get(c, {}) for c in hud},
            )
            self.view["project"] = self.projects.view()
        except Exception as exc:
            logger.debug(f"Minecraft project update failed: {exc}")
        self._state_at = time.time()
        self.hud = hud
        self.view["hud"] = hud
        now = time.time()
        if now - self._hud_sent >= 2:
            self._hud_sent = now
            await self._push({"kind": "hud", "hud": hud})

    # ------------------------------------------------------------ director
    async def _command(self, cid: str, command: str) -> bool:
        """A Mindcraft command that runs right away, without an AI call."""
        return await self.link.emit("send-message", self.names[cid], {"from": DIRECTOR, "message": command})

    def _friend(self, cid: str) -> str:
        return next((c for c in self.cast if c != cid), cid)

    def goal_for(self, cid: str) -> str:
        friend = self.names[self._friend(cid)]
        view = self.projects.view()
        base = self.projects.state.get("base")
        where = f" near the base at x {base[0]} z {base[2]}" if base else " near your base"
        now = self.projects.current()
        gather = now[1][1] if now else "gather useful materials and make the base prettier"
        text = (
            f"Team project with {friend}: {strip_emoji(view.get('title', ''))}, part {view.get('step', '')}. "
            f"{gather[0].upper() + gather[1:]}{where}, staying within 40 blocks of it and close to {friend}. "
            f"Use commands like collectBlocks and craftRecipe, give {friend} things she needs, "
            "and chat with her and the viewers in short lines while you work."
        )
        return re.sub(r'["!\\]', "", text)[:400]

    async def _set_goal(self, cid: str) -> None:
        self._goal_at[cid] = time.time()
        if await self._command(cid, f'!goal("{self.goal_for(cid)}")'):
            logger.info(f"Minecraft: {self.names[cid]} was given her goal again ({self._kind.get(cid) or 'starting'})")

    async def _direct(self, cid: str) -> None:
        """Keeps her doing something: a goal when she has none, out when she is stuck."""
        x, y, z = self._pos[cid]
        now = time.time()
        anchor = self._anchor.get(cid)
        if anchor is None or ((x - anchor[0]) ** 2 + (y - anchor[1]) ** 2 + (z - anchor[2]) ** 2) ** 0.5 > STILL_BLOCKS:
            self._anchor[cid] = (x, y, z, now)
            anchor = self._anchor[cid]
        kind = self._kind.get(cid, "")
        step = str(self.projects.view().get("step") or "")
        if step != self._goal_step:  # a new project part: both get the new goal and a kit
            self._goal_step = step
            self._goal_at = {}
            if not self.creative:
                asyncio.create_task(self._give_kits())
        if self.creative:
            return  # the builder loop flies them; nobody walks or gathers
        # Mindcraft stops a bot's goal for good after three replies without a
        # command; she then stands there until someone talks to her.
        if (kind in ("stopped", "idle") or cid not in self._goal_at) and now - self._goal_at.get(cid, 0) > GOAL_RETRY:
            await self._set_goal(cid)
            return
        if now - anchor[3] < STUCK_SECONDS or now - self._escaped_at.get(cid, 0) < STUCK_SECONDS:
            return
        await self._escape(cid)

    async def _give_kits(self) -> None:
        for cid in self.cast:
            for item, count in KIT:
                await rcon_command(f"give {self.names[cid]} minecraft:{item} {count}")
        logger.info("Minecraft: both got a kit of materials for the new project part")

    async def _escape(self, cid: str) -> None:
        """Same few blocks for a minute (a hole, a wall, a loop): out to the open, next to her friend."""
        now = time.time()
        self._escaped_at[cid] = now
        name = self.names[cid]
        friend = self._friend(cid)
        if self._kind.get(cid) == "chatting" and friend != cid:
            await self._command(cid, f'!endConversation("{self.names[friend]}")')
        await self._command(cid, "!stop")
        await asyncio.sleep(1.0)
        center = None
        friend_anchor = self._anchor.get(friend)
        if (
            friend != cid
            and friend in self._pos
            and now - self._seen_at.get(friend, 0) < 10
            and not (friend_anchor and now - friend_anchor[3] > STUCK_SECONDS)
        ):
            center = self._pos[friend]
        elif self.projects.state.get("base"):
            base = self.projects.state["base"]
            center = (float(base[0]), float(base[1]), float(base[2]))
        else:
            center = self._pos[cid]
        # spreadplayers lands on the top block of a free spot: out of holes,
        # caves and walls, never inside blocks.
        reply = await rcon_command(f"spreadplayers {center[0]:.0f} {center[2]:.0f} 0 6 false {name}", reply=True)
        ok = isinstance(reply, str) and "Spread" in reply
        if not ok and friend != cid:
            ok = await bring_into_view(name, self.names[friend])
        self._anchor.pop(cid, None)
        logger.info(f"Minecraft: {name} was stuck, {'moved out' if ok else 'could not be moved'} ({self._kind.get(cid) or '?'})")
        await self._reload_view(cid)
        await asyncio.sleep(1.0)
        await self._set_goal(cid)

    async def _reload_view(self, cid: str) -> None:
        """Her camera after a jump: reload it once the new chunks are there."""
        await self._push({"kind": "reload", "who": cid})
        if self.camera_on and cid == self.cam_focus:
            self._cam_sent_at = 0.0  # spectate again on the next camera tick

    # ------------------------------------------------------------ real game camera
    async def _camera_loop(self) -> None:
        """Your own Minecraft as the camera: spectator, through her eyes."""
        if not self.camera_player:
            return
        logger.info(f"Minecraft: join the world as {self.camera_player} with the real game to be the camera")
        while True:
            try:
                await self._camera_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug(f"Minecraft camera: {exc}")
            await asyncio.sleep(2.0)

    async def _camera_tick(self) -> None:
        online = await rcon_online()
        if online is None:
            return
        here = self.camera_player.lower() in online
        if here and not self.camera_on:
            await rcon_command(f"gamemode spectator {self.camera_player}")
            self.camera_on = True
            self._cam_sent_at = 0.0
            self.view["camera"] = "client"
            await self._push({"kind": "camera", "mode": "client"})
            logger.info(f"Minecraft: {self.camera_player} is the camera now (real game)")
        elif not here and self.camera_on:
            self.camera_on = False
            self.view["camera"] = "web"
            await self._push({"kind": "camera", "mode": "web"})
            logger.info(f"Minecraft: {self.camera_player} left, back to the web views")
        if not self.camera_on:
            return
        bots = [c for c in self.cast if self.names[c].lower() in online]
        if not bots:
            return
        if self.cam_focus not in bots:
            self.cam_focus = bots[0]
            self._cam_focus_at = time.time()
            self._cam_sent_at = 0.0
        if time.time() - self._cam_sent_at >= CAMERA_REFRESH:
            await self._spectate()

    async def _spectate(self) -> None:
        self._cam_sent_at = time.time()
        await rcon_command(f"spectate {self.names[self.cam_focus]} {self.camera_player}")
        await self._push({"kind": "focus", "who": self.cam_focus})

    async def _camera_to(self, cid: str) -> None:
        """The one who talks gets the camera (held CAMERA_HOLD so it does not flicker)."""
        if not self.camera_on or cid == self.cam_focus or time.time() - self._cam_focus_at < CAMERA_HOLD:
            return
        if time.time() - self._seen_at.get(cid, 0) > 15:
            return  # not in the world right now
        self.cam_focus = cid
        self._cam_focus_at = time.time()
        await self._spectate()

    # ------------------------------------------------------------ creative builders
    async def _build_loop(self) -> None:
        """Creative: the girls take turns flying to the next piece and building it."""
        if not self.creative:
            return
        watched_at = 0.0
        while True:
            await asyncio.sleep(1.0)
            try:
                here = [c for c in self.cast if time.time() - self._seen_at.get(c, 0) < 10]
                base = self.projects.state.get("base")
                if not here or not base or not self.link.connected.is_set() or self.projects.current() is None:
                    continue
                total = max(60.0, self.projects.minutes * 60)
                if self.projects.timed():  # the network trains: they hover in front of it and watch
                    if time.time() - watched_at > 60:
                        watched_at = time.time()
                        for i, cid in enumerate(here):
                            view, focus = (25, 16, -8 + 10 * i), (40, 16, -6 + 4 * i)  # back from the wall: the whole network in view
                            asyncio.create_task(self._arrive(cid, view, focus, await self._fly(cid, view, focus)))
                    progress = float(self.projects.state.get("progress", 0.0)) + 1.0 / total
                    self.projects.state["progress"] = progress
                    if progress >= 1.0:
                        await self.projects.finish_timed()
                    continue
                steps = self.projects.steps()
                done = int(self.projects.state.get("built", 0))
                if done >= len(steps):
                    await self.projects.advance()
                    continue
                step = steps[done]
                self._builder_turn += 1
                cid = here[self._builder_turn % len(here)]
                started = time.time()
                flight = await self._fly(cid, step["view"], step["focus"])
                await self._arrive(cid, step["view"], step["focus"], flight)
                if not await self.projects.run_step(step):
                    await asyncio.sleep(10)  # no server console yet
                    continue
                self.projects.state["built"] = done + 1
                self.projects.state["progress"] = (done + 1) / len(steps)
                self.projects._save()
                self.view["project"] = self.projects.view()
                await self._push({"kind": "project", **self.view["project"]})
                await self._camera_to(cid)
                pace = min(PIECE_SECONDS[1], max(PIECE_SECONDS[0], total / len(steps)))
                await asyncio.sleep(max(0.0, pace - (time.time() - started)))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"Minecraft: building failed: {exc}")
                await asyncio.sleep(5)

    async def _fly(self, cid: str, view: tuple, focus: tuple) -> float:
        """Send her flying to `view` (base relative), looking at `focus`. Returns about how long it takes."""
        bx, by, bz = self.projects.state["base"]
        x, y, z = bx + view[0], by + view[1], bz + view[2]
        lx, ly, lz = bx + focus[0], by + focus[1], bz + focus[2]
        here = self._pos.get(cid, (x, y, z))
        across = ((here[0] - x) ** 2 + (here[2] - z) ** 2) ** 0.5
        cruise = by + 22 if across > 12 else 0  # long trips go over the castle
        await self._command(cid, f"!flyTo({x:.1f}, {y:.1f}, {z:.1f}, {lx:.1f}, {ly:.1f}, {lz:.1f}, {cruise:.0f})")
        up = max(0.0, max(here[1] + 1, y + 3, cruise) - here[1])
        down = max(0.0, max(here[1] + 1, y + 3, cruise) - y)
        return min(10.0, (up + across + down) / 9.0 + 0.8)

    async def _arrive(self, cid: str, view: tuple, focus: tuple, flight: float) -> None:
        """Wait for the flight; if a hill or a tree stopped her (players cannot
        fly through blocks), put her there with a plain teleport."""
        await asyncio.sleep(flight)
        bx, by, bz = self.projects.state["base"]
        x, y, z = bx + view[0], by + view[1], bz + view[2]
        here = self._pos.get(cid)
        if here and ((here[0] - x) ** 2 + (here[1] - y) ** 2 + (here[2] - z) ** 2) ** 0.5 <= 3:
            return
        await rcon_command(f"tp {self.names[cid]} {x:.1f} {y:.1f} {z:.1f}")
        await asyncio.sleep(1.0)
        lx, ly, lz = bx + focus[0], by + focus[1], bz + focus[2]
        await self._command(cid, f"!flyTo({x:.1f}, {y:.1f}, {z:.1f}, {lx:.1f}, {ly:.1f}, {lz:.1f}, 0)")  # just to look
        await self._reload_view(cid)
        logger.info(f"Minecraft: {self.names[cid]} was blocked on the way, teleported to her spot")

    async def _tell_one(self, text: str) -> None:
        """A note for one girl (whoever spoke least), so only one AI call answers it."""
        cid = self._quietest(self._present())
        await self.link.emit("send-message", self.names[cid], {"from": "system", "message": text})

    async def _net_loop(self) -> None:
        """The real neural network: trains live, reads digits on the wall."""
        last = 0.0
        while True:
            await asyncio.sleep(2.0)
            if not self.net_show.requests and time.time() - last < 15:
                continue
            last = time.time()
            try:
                await self.net_show.tick()
                self.view["net"] = self.net_show.stats()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"Minecraft: neural network step failed: {exc}")

    async def _tell_both(self, text: str) -> None:
        for cid in self.cast:
            await self.link.emit("send-message", self.names[cid], {"from": "system", "message": text})
        for cid in self.cast:
            await self._react(cid, "celebrate")

    async def _keep_together(self) -> None:
        """Far apart for a minute: the one who wandered off walks back."""
        now = time.time()
        if len(self.cast) < 2 or not all(
            c in self._pos and now - self._seen_at.get(c, 0) < 10 for c in self.cast[:2]
        ):
            return  # someone is not in the world right now: nothing to do
        a, b = self.cast[0], self.cast[1]
        (ax, _ay, az), (bx, _by, bz) = self._pos[a], self._pos[b]
        far = ((ax - bx) ** 2 + (az - bz) ** 2) ** 0.5 > TOGETHER_BLOCKS
        now = time.time()
        if not far:
            self._apart_since = 0.0
            return
        self._apart_since = self._apart_since or now
        if now - self._apart_since < 20 or now - self._nudged_at < 45:
            return
        self._nudged_at = now
        # The one who spoke least recently moves (most likely not on camera).
        mover, friend = (a, b) if self._spoke_at.get(a, 0) <= self._spoke_at.get(b, 0) else (b, a)
        # Walking back across a desert took Luna the whole stream; a teleport
        # takes one second and keeps the show on the two of them together.
        if await bring_into_view(self.names[mover], self.names[friend]):
            logger.info(f"Minecraft: {self.names[mover]} teleported to {self.names[friend]}")
            self._anchor.pop(mover, None)
            await self._reload_view(mover)
            await self.link.emit(
                "send-message",
                self.names[mover],
                {"from": "system", "message": f"You were just teleported next to {self.names[friend]}. Say something to her and keep working together."},
            )
            return
        await self.link.emit(
            "send-message",
            self.names[mover],
            {
                "from": "system",
                "message": (
                    f"You drifted far away from {self.names[friend]}. Go back to her now with "
                    f'!goToPlayer("{self.names[friend]}", 3), say something to her, and keep working together.'
                ),
            },
        )
        logger.info(f"Minecraft: {self.names[mover]} walks back to {self.names[friend]}")

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
            if self.viewer_lines:  # answers to chat always go first
                line = self.viewer_lines.popleft()
                if time.time() - line["at"] <= VIEWER_LINE_MAX_AGE:
                    await self._say(line["who"], line["text"], pick_mood(line["text"]))
                continue
            if not self.lines:
                self.line_ready.clear()
                await self.line_ready.wait()
                continue
            line = self.lines.popleft()
            if time.time() - line["at"] > LINE_MAX_AGE:
                continue
            await self._say(line["who"], line["text"], pick_mood(line["text"]), short=True)

    async def _quick_reply(self, cid: str, author: str, text: str) -> None:
        started = time.time()
        line = await self.reply_text(cid, author, text)
        if not line:
            return
        self.viewer_lines.append({"who": cid, "text": line, "at": time.time()})
        self.line_ready.set()
        logger.info(f"Minecraft: {self.names[cid]} answers {author} ({time.time() - started:.1f}s): {line}")

    async def reply_text(self, cid: str, author: str, text: str) -> str:
        """One or two short spoken sentences from her to a viewer."""
        name = self.names[cid]
        friend = self.names[self._friend(cid)]
        doing = str((self.hud.get(cid) or {}).get("doing") or "playing").replace("action:", "")
        view = self.projects.view()
        persona = ""
        prof = self.session.room.get(cid)
        if prof is not None:
            persona = re.sub(r"\s+", " ", getattr(prof, "persona", "") or "").strip()[:300]
        recent = " | ".join(list(self.said)[-4:])
        system = (
            f"You are {name}. {persona} You are live on YouTube playing {'creative' if self.creative else 'survival'} Minecraft with {friend}. "
            f"Right now you are: {doing}. Team project: {strip_emoji(view.get('title', ''))}, working on {view.get('step', '')}. "
            f"Just said on stream: {recent or 'nothing yet'}. {self.net_show.describe()} "
            "A viewer wrote in the live chat. Answer them out loud in one or two short sentences, under 25 words. "
            "Say their name once. If they ask you to do something, say you will do it, or cheekily why not. "
            "Plain spoken words only: no emojis, no emoticons, no symbols, no hashtags, no commands, no quotes."
        )
        reply = ""
        key = (os.environ.get("OPENAI_API_KEY") or "").strip()
        if key:
            try:
                if self._llm is None:
                    from openai import AsyncOpenAI

                    self._llm = AsyncOpenAI(api_key=key, timeout=8.0, max_retries=1)
                response = await self._llm.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": f"{author} wrote: {text}"},
                    ],
                    max_tokens=70,
                    temperature=0.8,
                )
                reply = response.choices[0].message.content or ""
                try:
                    from ..vr_agent.usage import usage

                    usage.record_llm("minecraft chat answer")
                except Exception:
                    pass
            except Exception as exc:
                logger.warning(f"Minecraft: quick answer failed ({exc}), short answer instead")
        reply = strip_emoji(COMMAND_RE.sub(" ", reply)).strip().strip('"').strip()
        if not re.search(r"[A-Za-z]", reply):
            reply = random.choice(
                (
                    f"Hi {author}! I see you, give me a second!",
                    f"Oh, {author}! Good one, hold on!",
                    f"{author}, I hear you! Let me see what I can do.",
                )
            )
        return reply[:220]

    async def _say(self, cid: str, text: str, mood: str = "", short: bool = False) -> None:
        await self._wait_ready()
        if mood:
            await self._react(cid, mood)
        self.speaking = cid
        self._spoke_at[cid] = time.time()
        await self._camera_to(cid)
        self.said.append(f"{self.names[cid]}: {text[:120]}")
        try:
            parts = chunks(text)
            for i, chunk in enumerate(parts[:2] if short else parts):
                if short and i and self.viewer_lines:
                    break  # a viewer is waiting: the rest of the chatter can go
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


VIEWER_ORIGINAL = """    function botPosition () {
      const packet = { pos: bot.entity.position, yaw: bot.entity.yaw, addMesh: true }
      if (firstPerson) {
        packet.pitch = bot.entity.pitch
      }
      socket.emit('position', packet)
      worldView.updatePosition(bot.entity.position)
    }
"""
VIEWER_SMOOTH = """    // VR Agent: the camera turns smoothly instead of snapping with every head
    // turn, and looks down only half as far, so viewers keep the horizon.
    let camYaw = bot.entity.yaw
    let camPitch = bot.entity.pitch
    function botPosition () {
      worldView.updatePosition(bot.entity.position)
    }
    const smoothCamera = setInterval(() => {
      if (!bot.entity) return
      let d = bot.entity.yaw - camYaw
      d = Math.atan2(Math.sin(d), Math.cos(d))
      camYaw += d * 0.1
      camPitch += (bot.entity.pitch * 0.5 - camPitch) * 0.1
      const packet = { pos: bot.entity.position, yaw: camYaw, addMesh: true }
      if (firstPerson) packet.pitch = camPitch
      socket.emit('position', packet)
    }, 50)
    socket.on('disconnect', () => clearInterval(smoothCamera))
"""


# The viewer page itself: the camera used to jump to every position update
# (they come in bursts while the bot thinks), which looks like stop and go.
# Now it glides toward the latest position and turn on every drawn frame.
CAMERA_ORIGINAL = (
    'setFirstPersonCamera(t,e,i){if(t){let e=t.y+this.playerHeight;this.isSneaking&&(e-=.3),'
    'new r.Tween(this.camera.position).to({x:t.x,y:e,z:t.z},50).start()}this.camera.rotation.set(i,e,0,"ZYX")}'
)
CAMERA_GLIDE = (
    "setFirstPersonCamera(t,e,i){/*VR Agent glide*/const c=this.camera;"
    "if(t){let y=t.y+this.playerHeight;this.isSneaking&&(y-=.3);"
    "if(!this._ct)c.position.set(t.x,y,t.z);this._ct={x:t.x,y:y,z:t.z}}"
    'if(!this._cr)c.rotation.set(i,e,0,"ZYX");this._cr={p:i,y:e};'
    "if(!this._cl){this._cl=1;let last=performance.now();const step=(now)=>{"
    "const dt=Math.min(.1,(now-last)/1e3);last=now;const k=1-Math.exp(-dt*5);"
    "if(this._ct){c.position.x+=(this._ct.x-c.position.x)*k;c.position.y+=(this._ct.y-c.position.y)*k;"
    "c.position.z+=(this._ct.z-c.position.z)*k}"
    "let d=this._cr.y-c.rotation.y;d=Math.atan2(Math.sin(d),Math.cos(d));"
    'c.rotation.set(c.rotation.x+(this._cr.p-c.rotation.x)*k,c.rotation.y+d*k,0,"ZYX");'
    "requestAnimationFrame(step)};requestAnimationFrame(step)}}"
)


# Creative builders: the engine flies a girl to each spot she builds at (sent
# from DIRECTOR, so it runs without an AI call). Up first, across, then down,
# so the straight flight never cuts through the castle.
FLY_ANCHOR = """    {
        name: '!searchForBlock',"""
FLY_MARK = "    { // VR Agent: creative flight"
FLY_COMMANDS = FLY_MARK + """ for building on stream (v4)
        name: '!flyTo',
        description: 'Creative mode only: fly to x, y, z (over cruise height) and look at lx, ly, lz.',
        params: {
            'x': {type: 'float', description: 'x', domain: [-Infinity, Infinity]},
            'y': {type: 'float', description: 'y', domain: [-64, 320]},
            'z': {type: 'float', description: 'z', domain: [-Infinity, Infinity]},
            'lx': {type: 'float', description: 'look x', domain: [-Infinity, Infinity]},
            'ly': {type: 'float', description: 'look y', domain: [-64, 320]},
            'lz': {type: 'float', description: 'look z', domain: [-Infinity, Infinity]},
            'cruise': {type: 'float', description: 'fly over this height', domain: [-64, 320]}
        },
        perform: runAsAction(async (agent, x, y, z, lx, ly, lz, cruise) => {
            // Our own flight: mineflayer's flyTo waits for a move event that
            // never comes while hovering, and cannot be interrupted.
            const bot = agent.bot;
            const Vec3 = bot.entity.position.constructor;
            if (bot.pathfinder) bot.pathfinder.stop();
            bot.clearControlStates();
            bot.creative.startFlying();
            const p = bot.entity.position;
            const top = Math.max(p.y + 1, y + 3, cruise);
            const legs = [new Vec3(p.x, top, p.z), new Vec3(x, top, z), new Vec3(x, y, z)];
            for (const target of legs) {
                let best = Infinity;
                let since = 0;
                for (let i = 0; i < 600; i++) {
                    if (bot.interrupt_code) return;
                    const delta = target.minus(bot.entity.position);
                    const dist = Math.sqrt(delta.x * delta.x + delta.y * delta.y + delta.z * delta.z);
                    if (dist < 0.35) break;
                    // a block in the way (a tree, a hill): the server keeps her
                    // where she is, so stop trying this leg after a second
                    if (dist < best - 0.05) { best = dist; since = 0; } else if (++since > 20) break;
                    bot.entity.velocity = new Vec3(0, 0, 0);
                    bot.entity.position = bot.entity.position.plus(delta.scaled(Math.min(0.45, dist) / dist));
                    await new Promise((r) => setTimeout(r, 50));
                }
            }
            bot.entity.velocity = new Vec3(0, 0, 0);
            await bot.lookAt(new Vec3(lx, ly, lz), true);
            bot.swingArm('right');
        })
    },
    { // VR Agent: back on the ground
        name: '!land',
        description: 'Creative mode only: stop flying.',
        perform: async function (agent) {
            agent.bot.creative.stopFlying();
        }
    },
"""


def patch_mindcraft() -> list[str]:
    """Small, repeatable edits to Mindcraft's packages (safe to run every start).

    * prismarine viewer: smooth camera (no whiplash on stream)
    * pathfinder: walk instead of sprint, so moves are easy to follow
    * viewer: its own ports (VR_VIEWER_PORT_BASE), and a busy port no longer
      crashes the bot (Mika kept dropping out when port 3000 was taken)
    """
    done: list[str] = []
    edits = [
        (MINDCRAFT_DIR / "node_modules/prismarine-viewer/lib/mineflayer.js", VIEWER_ORIGINAL, VIEWER_SMOOTH),
        (MINDCRAFT_DIR / "node_modules/prismarine-viewer/public/index.js", CAMERA_ORIGINAL, CAMERA_GLIDE),
        (
            MINDCRAFT_DIR / "node_modules/prismarine-viewer/lib/mineflayer.js",
            "  http.listen(port, () => {",
            "  // VR Agent: a busy port must not crash the bot\n"
            "  http.on('error', (e) => console.error(`Prismarine viewer could not start on ${port}: ${e.message}`))\n"
            "  http.listen(port, () => {",
        ),
        (
            MINDCRAFT_DIR / "src/agent/vision/browser_viewer.js",
            "port: 3000+count_id,",
            "port: (parseInt(process.env.VR_VIEWER_PORT_BASE) || 3000) + count_id,",
        ),
        (
            MINDCRAFT_DIR / "node_modules/mineflayer-pathfinder/lib/movements.js",
            "    this.allowSprinting = true\n",
            "    this.allowSprinting = false // VR Agent: walk, easier to watch\n",
        ),
        (MINDCRAFT_DIR / "src/agent/commands/actions.js", FLY_ANCHOR, FLY_COMMANDS + FLY_ANCHOR),
    ]
    for path, old, new in edits:
        try:
            text = path.read_text()
        except Exception:
            continue
        if new.startswith(FLY_MARK) and FLY_MARK in text and new not in text:
            # an older version of our flight commands: take it out first
            start = text.index(FLY_MARK)
            text = text[:start] + text[text.index(FLY_ANCHOR, start):]
        if new in text:
            continue
        if old in text:
            text = text.replace(old, new, 1)
            path.write_text(text)
            done.append(path.name)
        else:
            logger.warning(f"Minecraft: {path.name} changed upstream, its stream patch was skipped")
    return done


# Commands the bots never need on stream. Fewer commands also means a shorter
# prompt on every single AI call.
BLOCKED_ACTIONS = [
    "!checkBlueprint", "!checkBlueprintLevel", "!getBlueprint", "!getBlueprintLevel",
    "!newAction", "!restart", "!clearChat", "!stfu", "!setMode", "!help", "!searchWiki",
    "!showVillagerTrades", "!tradeWithVillager", "!attackPlayer", "!digDown",
    "!lookAtPlayer", "!lookAtPosition",
]
ROBOT_LINE = "Be a friendly, casual, effective, and efficient robot."


def cache_friendly(default: str, style: str) -> str:
    """The system prompt with everything that never changes first.

    OpenAI reuses (and bills at a fraction) a prompt start it saw recently,
    but only up to the first changed character. Mindcraft puts the live stats
    near the top, so nothing was reused. Here the persona and the long command
    list come first; memory, stats, inventory and examples follow.
    """
    if not default:
        return ""
    body = default.replace("$COMMAND_DOCS\n", "").replace("$COMMAND_DOCS", "")
    body = body.replace("$SELF_PROMPT ", "").replace("$SELF_PROMPT", "")
    if ROBOT_LINE in body:
        body = body.replace(ROBOT_LINE, style)
    else:
        body = body.replace("\n", "\n" + style + " ", 1)
    marker = "\nSummarized memory:"
    if marker not in body:
        return body + "\n$COMMAND_DOCS\n$SELF_PROMPT"
    head, tail = body.split(marker, 1)
    return head + "\n$COMMAND_DOCS\n$SELF_PROMPT" + marker + tail


def _default_conversing() -> str:
    try:
        data = json.loads((MINDCRAFT_DIR / "profiles" / "defaults" / "_default.json").read_text())
        return str(data.get("conversing") or "")
    except Exception:
        return ""


RCON_PORT = 25575


def _rcon_password() -> str:
    path = RUNTIME_DIR / "rcon.txt"
    try:
        return path.read_text().strip()
    except Exception:
        import secrets

        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        password = secrets.token_hex(12)
        path.write_text(password)
        return password


def _set_properties(values: dict[str, str]) -> None:
    path = SERVER_DIR / "server.properties"
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return
    seen = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0]
        if key in values:
            out.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{k}={v}" for k, v in values.items() if k not in seen]
    path.write_text("\n".join(out) + "\n")


def enable_rcon() -> None:
    """Server console over the network, local only, for teleports."""
    _set_properties({
        "enable-rcon": "true",
        "rcon.port": str(RCON_PORT),
        "rcon.password": _rcon_password(),
        "broadcast-rcon-to-ops": "false",
    })


async def bring_into_view(mover: str, friend: str) -> bool:
    """Put `mover` a few blocks in front of `friend` so the
    camera (through the friend's eyes) shows both girls. Spots that are not
    free (a wall, a tree, a slope) are skipped, so nobody ends up inside
    blocks; a plain tp is the last resort."""
    for up, ahead in ((0, 3), (1, 3), (0, 2), (1, 2), (2, 3), (0, 4)):
        command = (
            f"execute at {friend} rotated ~ 0 positioned ^ ^{up} ^{ahead} "
            f"if block ~ ~ ~ minecraft:air if block ~ ~1 ~ minecraft:air "
            f"run tp {mover} ~ ~ ~"
        )
        reply = await rcon_command(command, reply=True)
        if reply is False:
            return False  # no RCON at all
        if isinstance(reply, str) and reply.startswith("Teleported"):
            return True
    return bool(await rcon_command(f"tp {mover} {friend}"))


async def rcon_online() -> Optional[set[str]]:
    """Lower case names of the players online, or None without RCON."""
    text = await rcon_command("list", reply=True)
    if not isinstance(text, str):
        return None
    names = text.split(":", 1)[1] if ":" in text else ""
    return {n.strip().lower() for n in names.split(",") if n.strip()}


async def rcon_command(command: str, reply: bool = False) -> Any:
    """Run one server command. False when the server has no RCON (yet)."""
    import struct

    def packet(pid: int, kind: int, body: str) -> bytes:
        data = struct.pack("<ii", pid, kind) + body.encode() + b"\x00\x00"
        return struct.pack("<i", len(data)) + data

    async def read(reader: Any) -> tuple[int, str]:
        size = struct.unpack("<i", await reader.readexactly(4))[0]
        data = await reader.readexactly(size)
        pid = struct.unpack("<i", data[:4])[0]
        return pid, data[8:-2].decode("utf-8", "replace")

    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", RCON_PORT), timeout=2)
    except Exception:
        return False
    try:
        writer.write(packet(1, 3, _rcon_password()))
        await writer.drain()
        pid, _body = await asyncio.wait_for(read(reader), timeout=3)
        if pid == -1:
            logger.warning("Minecraft: the server refused the RCON password")
            return False
        writer.write(packet(2, 2, command))
        await writer.drain()
        _pid, body = await asyncio.wait_for(read(reader), timeout=3)
        return body if reply else True
    except Exception as exc:
        logger.debug(f"Minecraft: RCON failed: {exc}")
        return False
    finally:
        writer.close()


def set_difficulty(level: str) -> None:
    if level not in ("peaceful", "easy", "normal", "hard"):
        return
    path = SERVER_DIR / "server.properties"
    try:
        lines = path.read_text().splitlines()
    except Exception:
        return
    out = [f"difficulty={level}" if line.startswith("difficulty=") else line for line in lines]
    if not any(line.startswith("difficulty=") for line in lines):
        out.append(f"difficulty={level}")
    path.write_text("\n".join(out) + "\n")


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
