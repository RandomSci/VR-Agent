"""A real neural network, written from scratch, that reads digits.

Mika and Luna "build" it in Minecraft (minecraft_net_show.py draws it in
their world), but the network itself is real: a multilayer perceptron with
35 inputs (a 5 x 7 pixel digit), two hidden layers of 8 ReLU neurons and 10
softmax outputs, trained with plain backpropagation and momentum SGD. Only
numpy, no ML library: every gradient below is written out by hand.

The digits are the classic 5 x 7 pixel font with "handwriting" noise: pixels
that drop out, stray pixels, and strokes that sit one pixel off. Training
data and test data are made from different random seeds, so the accuracy is
measured on digits the network never saw.

The weights, the epoch and the accuracy history are saved in
data/minecraft_net.json, so a restart keeps training the same network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import numpy as np

STATE_FILE = Path("data/minecraft_net.json")
WIDTH, HEIGHT = 5, 7
SIZES = (WIDTH * HEIGHT, 8, 8, 10)

# 5 x 7 digits, top row first.
FONT = {
    0: ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    1: ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    2: ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    3: ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    4: ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    5: ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    6: ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    7: ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    8: ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    9: ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
}


def clean_digit(digit: int) -> np.ndarray:
    return np.array([[int(c) for c in row] for row in FONT[digit]], dtype=np.float64)


def handwritten(digit: int, rng: np.random.Generator) -> np.ndarray:
    """One messy copy of a digit (7 x 5, values 0 or 1)."""
    img = clean_digit(digit)
    # a stroke row or column nudged one pixel (wobbly handwriting)
    if rng.random() < 0.35:
        r = rng.integers(HEIGHT)
        img[r] = np.roll(img[r], rng.choice((-1, 1)))
    if rng.random() < 0.2:
        c = rng.integers(WIDTH)
        img[:, c] = np.roll(img[:, c], rng.choice((-1, 1)))
    on = img > 0
    drop = (rng.random(img.shape) < 0.10) & on  # the pen skipped
    stray = (rng.random(img.shape) < 0.05) & ~on  # a smudge
    img[drop] = 0
    img[stray] = 1
    return img


def dataset(count: int, seed: int) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    labels = rng.integers(0, 10, size=count)
    images = np.stack([handwritten(int(d), rng).ravel() for d in labels])
    return images, labels


class DigitNet:
    """35 -> 8 -> 8 -> 10, ReLU, softmax, cross entropy, backprop by hand."""

    def __init__(self, seed: int = 7) -> None:
        rng = np.random.default_rng(seed)
        self.weights = [rng.normal(0, np.sqrt(2.0 / a), size=(a, b)) for a, b in zip(SIZES, SIZES[1:])]
        self.biases = [np.zeros(b) for b in SIZES[1:]]
        self._vw = [np.zeros_like(w) for w in self.weights]
        self._vb = [np.zeros_like(b) for b in self.biases]
        self.epoch = 0
        self.steps = 0  # mini-batch updates so far
        self.history: list[dict[str, float]] = []  # epoch, loss, accuracy
        self.lr = 0.02
        self.momentum = 0.9
        self._train = dataset(3000, seed=1)
        self._test = dataset(600, seed=2)
        self._rng = np.random.default_rng(seed + self.epoch)

    # ------------------------------------------------------------ forward
    def forward(self, x: np.ndarray) -> list[np.ndarray]:
        """Activations of every layer, input first. x is (n, 35) or (35,)."""
        acts = [np.atleast_2d(x)]
        for i, (w, b) in enumerate(zip(self.weights, self.biases)):
            z = acts[-1] @ w + b
            if i < len(self.weights) - 1:
                acts.append(np.maximum(z, 0.0))  # ReLU
            else:
                z = z - z.max(axis=1, keepdims=True)
                e = np.exp(z)
                acts.append(e / e.sum(axis=1, keepdims=True))  # softmax
        return acts

    def loss(self, x: np.ndarray, y: np.ndarray) -> float:
        probs = self.forward(x)[-1]
        return float(-np.mean(np.log(probs[np.arange(len(y)), y] + 1e-12)))

    # ------------------------------------------------------------ backward
    def gradients(self, x: np.ndarray, y: np.ndarray) -> tuple[list[np.ndarray], list[np.ndarray]]:
        """Backpropagation of the mean cross entropy loss."""
        acts = self.forward(x)
        n = len(y)
        delta = acts[-1].copy()
        delta[np.arange(n), y] -= 1.0  # d loss / d logits for softmax + cross entropy
        delta /= n
        grad_w: list[np.ndarray] = [np.empty(0)] * len(self.weights)
        grad_b: list[np.ndarray] = [np.empty(0)] * len(self.biases)
        for layer in range(len(self.weights) - 1, -1, -1):
            grad_w[layer] = acts[layer].T @ delta
            grad_b[layer] = delta.sum(axis=0)
            if layer:
                delta = (delta @ self.weights[layer].T) * (acts[layer] > 0)  # through the ReLU
        return grad_w, grad_b

    def _step(self, x: np.ndarray, y: np.ndarray) -> None:
        gw, gb = self.gradients(x, y)
        for i in range(len(self.weights)):
            self._vw[i] = self.momentum * self._vw[i] - self.lr * gw[i]
            self._vb[i] = self.momentum * self._vb[i] - self.lr * gb[i]
            self.weights[i] += self._vw[i]
            self.biases[i] += self._vb[i]

    def train_steps(self, batches: int, batch: int = 32) -> dict[str, float]:
        """A few mini-batch steps (the live show trains a little at a time)."""
        x, y = self._train
        for _ in range(batches):
            idx = self._rng.integers(0, len(y), size=batch)
            self._step(x[idx], y[idx])
        self.steps += batches
        self.epoch = self.steps * batch // len(y)
        return self._record()

    def train_epoch(self, batch: int = 32) -> dict[str, float]:
        x, y = self._train
        order = self._rng.permutation(len(y))
        for start in range(0, len(y), batch):
            idx = order[start:start + batch]
            self._step(x[idx], y[idx])
            self.steps += 1
        self.epoch = self.steps * batch // len(y)
        return self._record()

    def _record(self) -> dict[str, float]:
        stats = {
            "epoch": self.epoch,
            "steps": self.steps,
            "loss": round(self.loss(*self._train), 4),
            "accuracy": round(self.accuracy(), 4),
        }
        self.history.append(stats)
        self.history = self.history[-200:]
        return stats

    def accuracy(self) -> float:
        x, y = self._test
        return float(np.mean(self.forward(x)[-1].argmax(axis=1) == y))

    # ------------------------------------------------------------ use
    def predict(self, image: np.ndarray) -> dict[str, Any]:
        acts = self.forward(image.ravel())
        probs = acts[-1][0]
        guess = int(probs.argmax())
        return {
            "guess": guess,
            "confidence": float(probs[guess]),
            "probs": [float(p) for p in probs],
            "hidden": [a[0].tolist() for a in acts[1:-1]],
        }

    @property
    def last(self) -> dict[str, float]:
        if self.history:
            return self.history[-1]
        return {"epoch": 0, "steps": 0, "loss": round(self.loss(*self._train), 4), "accuracy": round(self.accuracy(), 4)}

    # ------------------------------------------------------------ saving
    def to_dict(self) -> dict[str, Any]:
        return {
            "sizes": list(SIZES),
            "epoch": self.epoch,
            "steps": self.steps,
            "weights": [w.tolist() for w in self.weights],
            "biases": [b.tolist() for b in self.biases],
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DigitNet":
        net = cls()
        if list(data.get("sizes") or []) == list(SIZES):
            net.weights = [np.array(w, dtype=np.float64) for w in data["weights"]]
            net.biases = [np.array(b, dtype=np.float64) for b in data["biases"]]
            net.epoch = int(data.get("epoch", 0))
            net.steps = int(data.get("steps", 0))
            net.history = list(data.get("history") or [])
            net._rng = np.random.default_rng(7 + net.steps)
        return net

    def save(self, path: Path = STATE_FILE) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.to_dict()))
        except Exception:
            pass

    @classmethod
    def load(cls, path: Path = STATE_FILE) -> "DigitNet":
        try:
            return cls.from_dict(json.loads(path.read_text()))
        except Exception:
            return cls()


def sample(digit: Optional[int] = None, seed: Optional[int] = None) -> tuple[int, np.ndarray]:
    """A fresh messy digit for a demo (never from the training set's seed)."""
    rng = np.random.default_rng(seed)
    d = int(rng.integers(0, 10)) if digit is None else int(digit) % 10
    return d, handwritten(d, rng)
