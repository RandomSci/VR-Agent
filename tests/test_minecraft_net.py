import asyncio

import numpy as np

from src.open_llm_vtuber.room import digit_net as dn
from src.open_llm_vtuber.room import minecraft_net_show as ns
from src.open_llm_vtuber.room import minecraft_projects as mp


def test_backpropagation_matches_numerical_gradients():
    net = dn.DigitNet()
    rng = np.random.default_rng(0)
    # off the ReLU corner: zero biases put some inputs exactly on it
    net.biases = [rng.normal(0, 0.1, size=b.shape) for b in net.biases]
    x, y = dn.dataset(16, seed=5)
    gw, gb = net.gradients(x, y)
    for layer in range(3):
        for _ in range(10):
            i, j = rng.integers(net.weights[layer].shape[0]), rng.integers(net.weights[layer].shape[1])
            old = net.weights[layer][i, j]
            net.weights[layer][i, j] = old + 1e-5
            up = net.loss(x, y)
            net.weights[layer][i, j] = old - 1e-5
            down = net.loss(x, y)
            net.weights[layer][i, j] = old
            numeric = (up - down) / 2e-5
            assert abs(numeric - gw[layer][i, j]) <= 1e-6 + 1e-4 * abs(numeric)
        b = net.biases[layer]
        old = b[0]
        b[0] = old + 1e-5
        up = net.loss(x, y)
        b[0] = old - 1e-5
        down = net.loss(x, y)
        b[0] = old
        assert abs((up - down) / 2e-5 - gb[layer][0]) < 1e-6


def test_it_really_learns_to_read_digits():
    net = dn.DigitNet()
    assert net.accuracy() < 0.3
    for _ in range(20):
        net.train_epoch()
    assert net.accuracy() > 0.9  # on test digits it never trained on
    right = sum(net.predict(img)["guess"] == d for d, img in (dn.sample(seed=500 + k) for k in range(100)))
    assert right >= 85


def test_saved_network_continues(tmp_path):
    net = dn.DigitNet()
    net.train_steps(20)
    path = tmp_path / "net.json"
    net.save(path)
    again = dn.DigitNet.load(path)
    assert again.steps == 20 and np.allclose(again.weights[1], net.weights[1])


def test_the_wall_shows_the_real_network():
    net = dn.DigitNet()
    for _ in range(10):
        net.train_epoch()
    digit, image = dn.sample(7, seed=3)
    commands, result = ns.demo_commands(net, digit, image)
    assert result["digit"] == 7 and 0 <= result["guess"] <= 9
    placed = [mp.place(c, (8, 62, -81), {}) for c in commands]
    assert all("{" not in c for c in placed)
    assert sum("glowstone" in c for c in commands) == 1  # the guess lights up
    lines = ns.weights(net)
    reds = sum("red_stained_glass" in c for c in lines)
    greens = sum("lime_stained_glass" in c for c in lines)
    assert reds and greens
    for build in (ns.build_input, ns.build_hidden, ns.build_output, ns.build_weights, ns.build_trained):
        for command in build():
            words = mp.place(command, (8, 62, -81), {}).split()
            if words[0] in ("fill", "setblock"):
                ys = [int(words[2])] + ([int(words[5])] if words[0] == "fill" else [])
                assert all(61 <= y <= 62 + 34 for y in ys), command
                if words[0] == "fill":
                    dx, dy, dz = (abs(int(words[i + 3]) - int(words[i])) + 1 for i in (1, 2, 3))
                    assert dx * dy * dz <= 32768


class Tracker:
    def __init__(self, project, milestone):
        self.state = {"project": project, "milestone": milestone, "base": [8, 62, -81]}


def test_requests_wait_until_the_network_exists_then_are_read(monkeypatch):
    async def run():
        told, pushed, commands = [], [], []

        async def rcon(cmd, reply=False):
            commands.append(cmd)
            return "ok"

        async def tell(text):
            told.append(text)

        async def push(op):
            pushed.append(op)

        monkeypatch.setattr(ns, "_net", dn.DigitNet())
        monkeypatch.setattr(dn.DigitNet, "save", lambda self, path=None: None)
        show = ns.NetShow(Tracker(1, 2), rcon, tell, push)
        assert not show.request(7, "Sam")  # still building the output layer
        show.tracker.state["milestone"] = 4  # training
        assert show.training and show.request(7, "Sam")
        await show.tick()
        assert show.last["digit"] == 7 and show.last["author"] == "Sam"
        assert any("Sam asked for a 7" in t for t in told)
        assert pushed[-1]["kind"] == "net" and pushed[-1]["steps"] == ns.TRAIN_BATCHES
        assert commands[0].startswith("forceload add")

    asyncio.run(run())
