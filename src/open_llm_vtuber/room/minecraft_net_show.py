"""The real neural network (digit_net.py) drawn in Mika and Luna's world.

On a black wall east of the castle, facing it (x is the base + NET_X):

    input board      hidden 1   hidden 2   outputs 0..9   prediction board
    (the digit,      8 neurons  8 neurons  10 neurons     (what it thinks,
     5 x 7 pixels)                                         green if right)

Neurons light up with their real activations, the weights between the
hidden layers and the outputs are glass lines colored by their real sign
(green positive, red negative; only the strongest 40 percent are drawn so it
stays readable). During the "Training" part of the project the network
really trains (backpropagation, a few mini-batches every tick), so the
accuracy climbs live. After that it reads a messy digit every two minutes,
or right away when chat asks ("draw 7", "predict 3").

All coordinates are relative to the base, "{x40}" style, filled in by
minecraft_projects.place().
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Optional

import numpy as np
from loguru import logger

from .digit_net import HEIGHT, SIZES, WIDTH, DigitNet, clean_digit, sample

NET_X = 40  # the neurons and boards; weights at NET_X + 1, the wall at NET_X + 2
TOP = 23  # top block of the boards
INPUT_Z = -30  # input board, 10 blocks wide
OUTPUT_BOARD_Z = 9  # prediction board
LAYER_Z = (-15, -6, 3)  # hidden 1, hidden 2, outputs (each neuron 2 x 2)
CENTER_Y = 17
SITE = (-33, 21)  # z range of the lab
TRAIN_BATCHES = 3  # mini-batches per tick while training
DEMO_EVERY = 900.0  # seconds between automatic demos once trained (viewers' "draw 7" any time)
GOOD_ENOUGH = 0.95  # stop training after this test accuracy

_net: Optional[DigitNet] = None


def get_net() -> DigitNet:
    global _net
    if _net is None:
        _net = DigitNet.load()
    return _net


def neuron_heights(count: int) -> list[int]:
    return [round(CENTER_Y + (i - (count - 1) / 2) * 3) for i in range(count)]


def _fill(z1: int, y1: int, z2: int, y2: int, block: str, x: int = NET_X) -> str:
    return f"fill {{x{x}}} {{y{y1}}} {{z{z1}}} {{x{x}}} {{y{y2}}} {{z{z2}}} minecraft:{block}"


def site() -> list[str]:
    z1, z2 = SITE
    return [
        f"fill {{x{NET_X - 6}}} {{y-1}} {{z{z1}}} {{x{NET_X + 4}}} {{y-1}} {{z{z2}}} minecraft:smooth_quartz",
        f"fill {{x{NET_X - 6}}} {{y}} {{z{z1}}} {{x{NET_X + 4}}} {{y34}} {{z{z2}}} minecraft:air",
        _fill(z1, 0, z2, 33, "black_concrete", x=NET_X + 2),
    ]


def board(z0: int, image: np.ndarray, on: str, off: str = "black_concrete") -> list[str]:
    out = []
    for r in range(HEIGHT):
        for c in range(WIDTH):
            y = TOP - 2 * r
            z = z0 + 2 * c
            out.append(_fill(z, y - 1, z + 1, y, on if image[r, c] > 0.5 else off))
    return out


def level(value: float, top: float) -> str:
    share = value / top if top > 1e-9 else 0.0
    if share > 0.66:
        return "sea_lantern"
    if share > 0.33:
        return "light_blue_concrete"
    if share > 0.05:
        return "blue_concrete"
    return "gray_concrete"


def neurons(layer: int, values: Optional[list[float]] = None, highlight: int = -1) -> list[str]:
    count = SIZES[layer + 1]
    z = LAYER_Z[layer]
    top = max(values) if values else 0.0
    out = []
    for i, y in enumerate(neuron_heights(count)):
        block = "gray_concrete" if values is None else level(values[i], top)
        if i == highlight:
            block = "glowstone"
        out.append(_fill(z, y, z + 1, y + 1, block))
    return out


def _line(z1: int, y1: int, z2: int, y2: int) -> list[tuple[int, int]]:
    steps = max(abs(z2 - z1), abs(y2 - y1), 1)
    return [(round(z1 + (z2 - z1) * t / steps), round(y1 + (y2 - y1) * t / steps)) for t in range(steps + 1)]


def weights(net: DigitNet, share: float = 0.4) -> list[str]:
    """Hidden 1 -> hidden 2 -> outputs as glass lines, colored by the real weights."""
    out = []
    blocks: dict[tuple[int, int], str] = {}
    for layer in (1, 2):
        za, zb = LAYER_Z[layer - 1] + 2, LAYER_Z[layer] - 1
        out.append(_fill(za, 0, zb, 33, "air", x=NET_X + 1))
        w = net.weights[layer]
        cut = np.quantile(np.abs(w), 1 - share)
        ya, yb = neuron_heights(w.shape[0]), neuron_heights(w.shape[1])
        pairs = sorted(((abs(w[i, j]), i, j) for i in range(w.shape[0]) for j in range(w.shape[1])))
        for size, i, j in pairs:  # strongest last, so they end up on top
            if size < cut:
                continue
            glass = "lime_stained_glass" if w[i, j] > 0 else "red_stained_glass"
            for pos in _line(za, ya[i] + 1, zb, yb[j] + 1):
                blocks[pos] = glass
    for (z, y), glass in blocks.items():
        out.append(f"setblock {{x{NET_X + 1}}} {{y{y}}} {{z{z}}} minecraft:{glass}")
    return out


def demo_commands(net: DigitNet, digit: int, image: np.ndarray) -> tuple[list[str], dict[str, Any]]:
    result = net.predict(image)
    guess = result["guess"]
    right = guess == digit
    out = board(INPUT_Z, image, "white_concrete")
    out += neurons(0, result["hidden"][0])
    out += neurons(1, result["hidden"][1])
    out += neurons(2, result["probs"], highlight=guess)
    out += board(OUTPUT_BOARD_Z, clean_digit(guess), "lime_concrete" if right else "red_concrete")
    return out, {**result, "digit": digit, "right": right}


# ------------------------------------------------------------ the project parts
def build_input() -> list[str]:
    _d, image = sample(seed=int(time.time()))
    return site() + board(INPUT_Z, image, "white_concrete")


def build_hidden() -> list[str]:
    return neurons(0) + neurons(1)


def build_output() -> list[str]:
    return neurons(2) + board(OUTPUT_BOARD_Z, np.zeros((HEIGHT, WIDTH)), "black_concrete", "gray_concrete")


def build_weights() -> list[str]:
    return weights(get_net())


def build_trained() -> list[str]:
    net = get_net()
    digit, image = sample(seed=int(time.time()))
    commands, _result = demo_commands(net, digit, image)
    return weights(net) + commands


class NetShow:
    """Trains the network live and makes it read digits on the wall."""

    def __init__(
        self,
        tracker: Any,
        rcon: Callable[..., Awaitable[Any]],
        tell: Callable[[str], Awaitable[None]],
        push: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.tracker = tracker
        self.rcon = rcon
        self.tell = tell
        self.push = push
        self.requests: list[tuple[int, str]] = []
        self._ticks = 0
        self._demo_at = 0.0
        self._loaded = False
        self.last: dict[str, Any] = {}

    # ------------------------------------------------------------ phase
    def _index(self) -> int:
        from .minecraft_projects import PROJECTS

        return next((i for i, p in enumerate(PROJECTS) if p["id"] == "neural_net"), -1)

    @property
    def training(self) -> bool:
        s = self.tracker.state
        return int(s.get("project", 0)) == self._index() and int(s.get("milestone", 0)) == 4

    @property
    def ready(self) -> bool:
        """The weights are on the wall: it can read digits."""
        s = self.tracker.state
        p = int(s.get("project", 0))
        return p > self._index() or (p == self._index() and int(s.get("milestone", 0)) >= 4)

    def request(self, digit: int, author: str) -> bool:
        if not self.ready:
            return False
        self.requests = (self.requests + [(digit % 10, author)])[-5:]
        return True

    def stats(self) -> dict[str, Any]:
        net = get_net()
        last = net.last
        return {
            "kind": "net",
            "training": self.training,
            "steps": int(last.get("steps", net.steps)),
            "epoch": int(last.get("epoch", net.epoch)),
            "loss": float(last.get("loss", 0.0)),
            "accuracy": float(last.get("accuracy", 0.0)),
            "last": self.last,
        }

    def describe(self) -> str:
        """One line for the girls' chat answers."""
        if not self.ready:
            return ""
        s = self.stats()
        text = (
            "Your neural network is real (written from scratch: 35 input pixels, two hidden layers of 8 neurons, "
            f"10 outputs, trained with backpropagation), {s['accuracy'] * 100:.0f} percent accurate after "
            f"{s['steps']} training steps."
        )
        if self.last:
            text += f" Last time it read a {self.last['digit']} and guessed {self.last['guess']}."
        return text

    # ------------------------------------------------------------ live
    async def tick(self) -> None:
        if not self.ready:
            return
        base = self.tracker.state.get("base")
        if not base:
            return
        net = get_net()
        if not self._loaded:  # keep the lab loaded so drawing works from anywhere
            await self._command(f"forceload add {{x{NET_X - 6}}} {{z{SITE[0]}}} {{x{NET_X + 4}}} {{z{SITE[1]}}}")
            self._loaded = True
        self._ticks += 1
        if self.training or net.last.get("accuracy", 0.0) < GOOD_ENOUGH and net.steps < 3000:
            stats = net.train_steps(TRAIN_BATCHES)
            net.save()
            if self._ticks % 4 == 0:
                await self._run(weights(net))
            if self.training and self._ticks % 32 == 0:
                await self.tell(
                    f"Neural network training update: {stats['steps']} backpropagation steps, loss {stats['loss']:.2f}, "
                    f"accuracy {stats['accuracy'] * 100:.0f} percent on digits it never saw. React in one short line."
                )
        now = time.time()
        # during Training the girls draw digits themselves (minecraft_mode._teach_round)
        if self.requests or (now - self._demo_at >= DEMO_EVERY and not self.training):
            await self.demo()
        await self.push(self.stats())

    async def demo(self) -> None:
        digit: Optional[int] = None
        author = ""
        if self.requests:
            digit, author = self.requests.pop(0)
        d, image = sample(digit)
        await self.read(d, image, author)

    async def read(self, d: int, image: np.ndarray, author: str = "", drawn_by: str = "") -> None:
        """The network reads this digit image (drawn by a girl, or asked for by chat)."""
        net = get_net()
        commands, result = demo_commands(net, d, image)
        await self._run(commands)
        self._demo_at = time.time()
        self.last = {
            "digit": d,
            "guess": result["guess"],
            "confidence": round(result["confidence"], 3),
            "right": result["right"],
            "author": author or drawn_by,
        }
        logger.info(f"Minecraft: the neural network read a {d} and guessed {result['guess']} ({result['confidence']:.0%})")
        await self.push(self.stats())
        if not (author or drawn_by):
            return  # an automatic demo: shown on screen, the girls are not asked to talk about it
        who = f" {author} asked for a {d}." if author else (f" {drawn_by} drew it." if drawn_by else "")
        verdict = "It got it RIGHT" if result["right"] else "It got it WRONG"
        await self.tell(
            f"Your neural network just read a messy digit on the input board.{who} The real digit is {d}, the network "
            f"guessed {result['guess']}, {result['confidence'] * 100:.0f} percent sure. {verdict}. "
            "React to it out loud in one short line, like proud or embarrassed parents."
        )
        await self.push(self.stats())

    async def _command(self, command: str) -> Any:
        from .minecraft_projects import place

        return await self.rcon(place(command, tuple(self.tracker.state["base"]), {}), reply=True)

    async def _run(self, commands: list[str]) -> None:
        for command in commands:
            reply = await self._command(command)
            if reply is False:
                return
