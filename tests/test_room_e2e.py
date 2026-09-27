"""Headless browser test of the room page against the harness server.

Proves in a real browser that both Live2D characters load independently,
the renderer detects look parameters per model, attention ops turn heads,
audio plays on the right character and the page reports playback complete,
and a disabled room falls back to the classic livestream page.

Skipped when no Chromium is available. Set VR_TEST_CHROMIUM to a Chromium
binary to use a specific one.
"""

from __future__ import annotations

import base64
import io
import os
import shutil
import socket
import threading
import time
import wave
from pathlib import Path

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")
httpx = pytest.importorskip("httpx")

from tests.harness.room_server import create_app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def chromium_path() -> str | None:
    env = os.environ.get("VR_TEST_CHROMIUM")
    if env and Path(env).exists():
        return env
    for candidate in sorted(
        Path("/opt/pw-browsers").glob("chromium-*/chrome-linux/chrome")
    ):
        return str(candidate)
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, room_dir: Path | None = None):
        self.port = free_port()
        self.app = create_app(room_dir)
        config = uvicorn.Config(
            self.app, host="127.0.0.1", port=self.port, log_level="warning"
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 20
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(timeout=10)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def post(self, path: str, body: dict) -> None:
        httpx.post(self.base + path, json=body, timeout=10).raise_for_status()


def tone_payload(character: str) -> dict:
    rate = 16000
    frames = b"".join(
        int(8000 * ((i // 400) % 2)).to_bytes(2, "little", signed=True)
        for i in range(rate)
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return {
        "type": "audio",
        "audio": base64.b64encode(buf.getvalue()).decode(),
        "volumes": [1.0 if (i // 1) % 2 else 0.2 for i in range(50)],
        "slice_length": 20,
        "display_text": {"text": "Testing one two.", "name": character},
        "actions": {"expressions": [1]},
        "character": character,
        "emotion_mode": "profile",
    }


@pytest.fixture(scope="module")
def browser():
    path = chromium_path()
    if not path:
        pytest.skip("no Chromium available")
    with playwright_api.sync_playwright() as p:
        b = p.chromium.launch(
            executable_path=path,
            args=[
                "--use-gl=angle",
                "--use-angle=swiftshader",
                "--enable-unsafe-swiftshader",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        yield b
        b.close()


_OPEN_PAGES: list = []


def open_room(browser, server: Server):
    # Close pages from earlier tests: each runs two Live2D models and music,
    # and piling them up slows the headless browser until pages time out.
    while _OPEN_PAGES:
        try:
            _OPEN_PAGES.pop().close()
        except Exception:
            pass
    page = browser.new_page(viewport={"width": 960, "height": 540})
    _OPEN_PAGES.append(page)
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(server.base + "/vr-agent/room.html")
    page.wait_for_function(
        "window.vrRoom && vrRoom.state().characters.length === 2 && "
        "vrRoom.state().characters.every(c => c.loaded || c.failed)",
        timeout=120000,
    )
    return page, errors


def test_two_characters_render_look_at_each_other_and_speak(browser):
    with Server() as server:
        page, errors = open_room(browser, server)
        state = page.evaluate("vrRoom.state()")
        assert [c["id"] for c in state["characters"]] == ["mika", "luna"]
        for c in state["characters"]:
            assert c["loaded"] and c["look"] == {
                "head": True,
                "eyes": True,
                "body": True,
            }
        assert state["characters"][0]["mouth"] == "ParamA"
        assert state["characters"][1]["mouth"] == "ParamMouthOpenY"

        # The server learns which models loaded.
        status = httpx.get(server.base + "/harness/clients").json()
        assert list(status["clients"].values())[0]["loaded"] == ["mika", "luna"]

        # Attention: Mika (left) looks right at Luna, Luna looks left at Mika.
        server.post(
            "/harness/push",
            {
                "ops": [
                    {
                        "op": "attention",
                        "character": "mika",
                        "target": "CHARACTER:luna",
                        "hold_ms": 5000,
                    },
                    {
                        "op": "attention",
                        "character": "luna",
                        "target": "CHARACTER:mika",
                        "hold_ms": 5000,
                    },
                ]
            },
        )
        page.wait_for_function(
            "vrRoom.state().characters[0].focus.x > 0.5 && vrRoom.state().characters[1].focus.x < -0.5",
            timeout=5000,
        )
        # Both look down toward the game board.
        server.post(
            "/harness/push",
            {
                "ops": [
                    {
                        "op": "attention",
                        "character": "mika",
                        "target": "GAME",
                        "hold_ms": 5000,
                    },
                    {
                        "op": "attention",
                        "character": "luna",
                        "target": "OBJECT:game_board",
                        "hold_ms": 5000,
                    },
                ]
            },
        )
        page.wait_for_function(
            "vrRoom.state().characters.every(c => c.focus.y < -0.3)", timeout=5000
        )
        # A summoned rabbit becomes a room object both characters look at
        # once it appears during the motion.
        httpx.post(
            server.base + "/harness/play",
            json={"character": "mika", "action": "summon_rabbit"},
        ).raise_for_status()
        page.wait_for_function(
            "vrRoom.state().characters.every(c => c.target === 'OBJECT:rabbit')",
            timeout=15000,
        )
        # Invalid ops are ignored without errors.
        server.post(
            "/harness/push",
            {
                "ops": [
                    {
                        "op": "attention",
                        "character": "mika",
                        "target": "javascript:alert(1)",
                    }
                ]
            },
        )

        # Speech on Luna: Luna speaks, Mika looks at her, playback is reported.
        server.post(
            "/harness/push",
            {
                "ops": [
                    {
                        "op": "attention",
                        "character": "mika",
                        "target": "VIEWER",
                        "hold_ms": 500,
                    }
                ]
            },
        )
        time.sleep(0.8)
        server.post("/harness/send", tone_payload("luna"))
        server.post("/harness/send", {"type": "backend-synth-complete"})
        page.wait_for_function("vrRoom.state().characters[1].speaking", timeout=15000)
        assert page.evaluate("vrRoom.state().characters[0].speaking") is False
        page.wait_for_function(
            "vrRoom.state().characters[0].target === 'CHARACTER:luna'", timeout=5000
        )
        deadline = time.time() + 20
        kinds: list[str] = []
        while time.time() < deadline:
            kinds = [
                m["type"] for m in httpx.get(server.base + "/harness/received").json()
            ]
            if "frontend-playback-complete" in kinds:
                break
            time.sleep(0.2)
        assert "audio-play-start" in kinds and "frontend-playback-complete" in kinds
        assert errors == []
        page.close()


def test_disabled_room_falls_back_to_classic_page(browser, tmp_path):
    room_dir = tmp_path / "room"
    shutil.copytree(ROOT / "room", room_dir)
    text = (
        (room_dir / "room.yaml").read_text().replace("enabled: true", "enabled: false")
    )
    (room_dir / "room.yaml").write_text(text)
    with Server(room_dir) as server:
        page = browser.new_page()
        page.goto(server.base + "/vr-agent/room.html?subtitles=0")
        page.wait_for_url("**/?mode=live&subtitles=0", timeout=20000)
        page.close()


def test_trivia_battle_plays_on_the_board_with_chat_and_sound(browser):
    """A viewer starts Trivia Battle; the board shows the question, a viewer
    answers first, the score and feed update, sound effect ops arrive, and a
    stop command removes the board. Characters look at the board."""
    with Server() as server:
        page, errors = open_room(browser, server)
        page.evaluate(
            "() => { window.__sfx = []; const a = HTMLMediaElement.prototype.play;"
            "HTMLMediaElement.prototype.play = function () {"
            "  if (String(this.src).includes('/sfx/')) window.__sfx.push(String(this.src).split('/').pop());"
            "  return a.call(this); }; }"
        )
        consumed = httpx.post(
            server.base + "/harness/chat", json={"user": "@ana", "text": "play trivia"}
        ).json()
        assert consumed["consumed"] is True
        page.wait_for_selector(".vrb-board:not([hidden])", timeout=5000)
        assert page.inner_text(".vrb-title") == "TRIVIA BATTLE"
        page.wait_for_function(
            "document.querySelector('.vrb-round').textContent === 'Round 1/5'",
            timeout=8000,
        )
        question = page.inner_text(".vrb-question")
        assert question.endswith("?")
        # Both characters turn toward the board when the question appears.
        page.wait_for_function(
            "vrRoom.state().characters.every(c => c.focus.y < -0.2)", timeout=4000
        )

        session = server.app.state.session
        answer = session.show.engine.active.question["correct_answer"]
        wrong = session.show.engine.active.question["wrong_answers"][0]
        assert httpx.post(
            server.base + "/harness/chat", json={"user": "@ben", "text": wrong}
        ).json()["consumed"]
        assert httpx.post(
            server.base + "/harness/chat", json={"user": "@cy", "text": answer}
        ).json()["consumed"]
        page.wait_for_function(
            "document.querySelector('.vrb-status').textContent === '@cy got it first!'",
            timeout=4000,
        )
        chips = page.eval_on_selector_all(
            ".vrb-chip", "els => els.map(e => e.textContent)"
        )
        assert chips[-1].replace(" ", "").upper() == "CHAT1"
        feed = page.inner_text(".vrb-feed")
        assert "@ben" in feed and "@cy" in feed
        assert page.inner_text(".vrb-reveal").startswith("Answer:")
        page.wait_for_function("window.__sfx.length >= 2", timeout=4000)
        assert "game_start.wav" in page.evaluate("window.__sfx")

        # "what games" is answered from the registry and spoken, no LLM.
        from open_llm_vtuber.vr_agent.usage import usage

        assert httpx.post(
            server.base + "/harness/chat",
            json={"user": "@dee", "text": "what games can you play?"},
        ).json()["consumed"]
        httpx.post(
            server.base + "/harness/chat",
            json={"user": "@dee", "text": "stop the game"},
        )
        page.wait_for_selector(".vrb-board", state="hidden", timeout=5000)
        assert usage.snapshot()["llm_requests"] == 0
        assert not errors


def test_trivia_starts_from_a_natural_conversation(browser, tmp_path):
    """The exact chat from a real stream: the viewer asks what games there
    are, how trivia works, then says "Sure". The board must appear, and none
    of this may call the LLM (rules come from the game's config.yaml)."""
    from open_llm_vtuber.vr_agent.usage import usage

    usage.reset()
    with Server() as server:
        page, errors = open_room(browser, server)
        page.set_viewport_size({"width": 1280, "height": 720})
        for text in (
            "what games can you play?",
            "Trivia game? How does it work? I'm interested",
        ):
            assert httpx.post(
                server.base + "/harness/chat", json={"user": "@selwyn", "text": text}
            ).json()["consumed"], text
        assert page.locator(".vrb-board:not([hidden])").count() == 0
        assert httpx.post(
            server.base + "/harness/chat", json={"user": "@selwyn", "text": "Sure"}
        ).json()["consumed"]
        page.wait_for_selector(".vrb-board:not([hidden])", timeout=5000)
        page.wait_for_function(
            "document.querySelector('.vrb-round').textContent === 'Round 1/5'",
            timeout=8000,
        )
        assert page.inner_text(".vrb-question").endswith("?")
        # The stage followed the viewport change without a reload.
        box = page.evaluate(
            "() => document.getElementById('vr-room-world').getBoundingClientRect().width"
            " / vrRoom.state().camera.zoom"
        )
        assert abs(box - 1280) < 4, box
        page.wait_for_timeout(700)  # let the question fade in before the picture
        shot = os.environ.get("VR_SCREENSHOT_DIR")
        if shot:
            page.screenshot(path=str(Path(shot) / "trivia-board.png"))
        assert usage.snapshot()["llm_requests"] == 0
        assert not errors


def test_background_music_loops_ducks_switches_for_games_and_survives_resize(browser):
    """Room music plays quietly from the saved ElevenLabs tracks (no API
    request), ducks while a character speaks, switches to the game track while
    the board is up, and keeps playing through a viewport change."""
    from open_llm_vtuber.vr_agent.usage import usage

    usage.reset()
    with Server() as server:
        page, errors = open_room(browser, server)
        page.wait_for_function(
            "(() => { const m = vrRoom.state().music; return m && m.context === 'running'"
            " && m.loaded.includes('room') && m.gains.room > 0.135; })()",
            timeout=20000,
        )
        music = page.evaluate("vrRoom.state().music")
        assert music["track"] == "room" and not music["failed"]
        full = music["gains"]["room"]
        assert abs(full - 0.14) < 0.02  # room.yaml music.volume

        # A character speaks: the music ducks, then comes back.
        server.post("/harness/send", tone_payload("luna"))
        page.wait_for_function(
            "vrRoom.state().music.ducked && vrRoom.state().music.gains.room < 0.07",
            timeout=5000,
        )
        server.post("/harness/send", {"type": "backend-synth-complete"})
        page.wait_for_function(
            "!vrRoom.state().music.ducked && vrRoom.state().music.gains.room > 0.12",
            timeout=8000,
        )

        # A game starts: the game track takes over, the room track fades out.
        httpx.post(
            server.base + "/harness/chat", json={"user": "@ana", "text": "play trivia"}
        )
        page.wait_for_function(
            "vrRoom.state().music.track === 'game' && (vrRoom.state().music.gains.game || 0) > 0.1",
            timeout=10000,
        )
        # Rotate to portrait and back: the same game track keeps playing.
        page.set_viewport_size({"width": 540, "height": 960})
        page.wait_for_timeout(400)
        page.set_viewport_size({"width": 960, "height": 540})
        page.wait_for_timeout(400)
        after = page.evaluate("vrRoom.state().music")
        assert after["track"] == "game" and after["gains"]["game"] > 0.1
        assert after["context"] == "running"

        httpx.post(
            server.base + "/harness/chat",
            json={"user": "@ana", "text": "stop the game"},
        )
        page.wait_for_function(
            "vrRoom.state().music.track === 'room' && vrRoom.state().music.gains.room > 0.1",
            timeout=10000,
        )
        # Music itself never calls an API. (TTS here is only the game lines
        # spoken because a viewer just chatted.)
        assert usage.snapshot()["llm_requests"] == 0
        assert not errors


def _shot(page, name):
    shot = os.environ.get("VR_SCREENSHOT_DIR")
    if shot:
        page.screenshot(path=str(Path(shot) / name))


def test_tic_tac_toe_and_rock_paper_scissors_render_and_play(browser):
    """Chat plays Luna at Tic-Tac-Toe (votes show on the cells, X lands), then
    Rock Paper Scissors against Mika (hands hidden until the reveal)."""
    from open_llm_vtuber.vr_agent.usage import usage

    usage.reset()
    with Server() as server:
        page, errors = open_room(browser, server)
        page.set_viewport_size({"width": 1280, "height": 720})

        def chat(user, text):
            return httpx.post(
                server.base + "/harness/chat", json={"user": user, "text": text}
            ).json()

        assert chat("@selwyn", "Luna, play tic tac toe with us")["consumed"]
        page.wait_for_selector(".vrb-board:not([hidden]) .vrg-grid", timeout=5000)
        assert page.inner_text(".vrb-title") == "TIC-TAC-TOE"
        game = server.app.state.session.show.engine.active
        page.wait_for_function(
            "document.querySelector('.vrb-turn') && "
            "document.querySelector('.vrb-turn').textContent === \"CHAT'S TURN\"",
            timeout=8000,
        )
        # The grid is square.
        box = page.eval_on_selector(
            ".vrg-grid",
            "e => [e.getBoundingClientRect().width, e.getBoundingClientRect().height]",
        )
        assert abs(box[0] - box[1]) < 2, box
        # Nothing on the board is cut off: the grid and the scores fit inside it.
        fits = page.evaluate(
            "() => { const b = document.querySelector('.vrb-board').getBoundingClientRect();"
            " return ['.vrg-grid', '.vrb-scores', '.vrb-status'].every(sel => {"
            " const r = document.querySelector(sel).getBoundingClientRect();"
            " return r.top >= b.top - 1 && r.bottom <= b.bottom + 1; }); }"
        )
        assert fits
        assert chat("@selwyn", "5")["consumed"]
        assert chat("@ana", "5")["consumed"]
        page.wait_for_function(
            "document.querySelectorAll('.vrg-cell')[4].querySelector('.vrg-votes').textContent === '2'",
            timeout=3000,
        )
        _shot(page, "tictactoe-vote.png")
        page.wait_for_function(
            "document.querySelectorAll('.vrg-cell')[4].querySelector('.vrg-mark').textContent === 'X'",
            timeout=8000,
        )
        page.wait_for_function(
            "[...document.querySelectorAll('.vrg-mark')].filter(m => m.textContent === 'O').length === 1",
            timeout=8000,
        )
        assert game.cells[4] == "X"
        page.wait_for_timeout(500)
        _shot(page, "tictactoe-board.png")

        chat("@selwyn", "stop the game")
        page.wait_for_selector(".vrb-board", state="hidden", timeout=5000)

        assert chat("@selwyn", "Mika, play rock paper scissors with us")["consumed"]
        page.wait_for_selector(".vrb-board:not([hidden]) .vrr-arena", timeout=5000)
        assert page.inner_text(".vrb-title") == "ROCK PAPER SCISSORS"
        page.wait_for_function(
            "document.querySelector('.vrr-votes') && !document.querySelector('.vrr-votes').hidden",
            timeout=8000,
        )
        assert chat("@selwyn", "rock")["consumed"]
        assert chat("@ana", "paper")["consumed"]
        assert chat("@ben", "rock")["consumed"]
        page.wait_for_timeout(300)
        _shot(page, "rps-vote.png")
        page.wait_for_function(
            "[...document.querySelectorAll('.vrr-hand')].every(h => !h.classList.contains('vrr-hidden'))",
            timeout=8000,
        )
        rps = server.app.state.session.show.engine.active
        assert rps.throws["viewers"] == "rock"
        page.wait_for_timeout(500)
        _shot(page, "rps-reveal.png")
        assert usage.snapshot()["llm_requests"] == 0
        assert not errors


def test_characters_glance_at_chat_walk_jump_dance_and_stay_home_in_games(browser):
    """A comment makes someone glance at chat at once. Characters walk, jump
    and dance inside their own half of the stage, and go home and stay there
    while a game is on. All of it runs in the browser with zero LLM calls."""
    from open_llm_vtuber.vr_agent.usage import usage

    usage.reset()
    with Server() as server:
        page, errors = open_room(browser, server)
        state = lambda: page.evaluate("vrRoom.state()")  # noqa: E731
        stage = {s["id"]: s for s in state()["stage"]}
        mika, luna = stage["mika"], stage["luna"]
        left, right = sorted([mika, luna], key=lambda s: s["home"])
        # Lanes hold each home and never overlap, so they cannot bump into each other.
        for s in (mika, luna):
            assert s["lane"][0] <= s["home"] <= s["lane"][1]
        assert left["lane"][1] < right["lane"][0]

        def settle(cid):
            page.wait_for_function(
                f"vrRoom.state().stage.find(s => s.id === '{cid}').move === null",
                timeout=10000,
            )
            return {s["id"]: s for s in state()["stage"]}[cid]

        # Walk to the far edge of her lane (a request past it is clamped).
        far = left["lane"][0] - 500
        assert page.evaluate(f"vrRoom.move('{left['id']}', 'walk', {{x: {far}}})")
        page.wait_for_timeout(300)
        mid = {s["id"]: s for s in state()["stage"]}[left["id"]]
        assert mid["move"] == "walk"
        after = settle(left["id"])
        # Positions are clamped to the stage (lanes no longer limit requested moves).
        assert after["x"] == round(1920 * 0.06) and after["x"] != left["home"]
        _shot(page, "moves-walked.png")
        assert page.evaluate(f"vrRoom.move('{left['id']}', 'home')")
        after = settle(left["id"])
        assert abs(after["x"] - left["home"]) <= 1

        # A jump goes up and lands back where she stood.
        assert page.evaluate(f"vrRoom.move('{right['id']}', 'jump', {{hops: 1}})")
        page.wait_for_timeout(310)
        airborne = {s["id"]: s for s in state()["stage"]}[right["id"]]
        assert airborne["y"] < right["y"] - 40
        assert settle(right["id"])["y"] == right["y"]
        assert page.evaluate(f"vrRoom.move('{right['id']}', 'dance', {{ms: 2000}})")
        settle(right["id"])

        # A comment arrives: someone looks at chat right away.
        httpx.post(server.base + "/harness/push", json={"ops": [{"op": "chat_seen"}]})
        page.wait_for_function(
            "vrRoom.state().characters.some(c => c.target === 'CHAT')", timeout=3000
        )

        # Mika wanders off, then a game starts: she walks home and nobody moves.
        assert page.evaluate(f"vrRoom.move('{left['id']}', 'walk', {{x: {far}}})")
        page.wait_for_timeout(250)
        httpx.post(
            server.base + "/harness/chat",
            json={"user": "@selwyn", "text": "play tic tac toe"},
        )
        page.wait_for_function("vrRoom.state().gameActive === true", timeout=5000)
        page.wait_for_function(
            "vrRoom.state().stage.every(s => s.move === null && Math.abs(s.x - s.home) <= 1)",
            timeout=5000,
        )
        assert (
            page.evaluate(f"vrRoom.move('{left['id']}', 'walk', {{x: {far}}})") is False
        )
        assert page.evaluate(f"vrRoom.move('{right['id']}', 'dance')") is False
        page.wait_for_timeout(1500)  # ambient beats keep them in place too
        assert all(abs(s["x"] - s["home"]) <= 1 for s in state()["stage"])

        httpx.post(
            server.base + "/harness/chat",
            json={"user": "@selwyn", "text": "stop the game"},
        )
        page.wait_for_function("vrRoom.state().gameActive === false", timeout=5000)
        assert page.evaluate(f"vrRoom.move('{right['id']}', 'jump')")
        assert usage.snapshot()["llm_requests"] == 0
        assert not errors


def test_world_objects_render_from_state_and_magic_removes_them(browser):
    """What is on screen = what RoomState holds: a frog spawned in the state
    is drawn, Mika's magic makes it vanish on screen and in the state, and
    a viewer's 'walk towards the center' glides her there (no LLM)."""
    from open_llm_vtuber.room.state import SceneState
    from open_llm_vtuber.vr_agent.usage import usage

    usage.reset()
    with Server() as server:
        page, errors = open_room(browser, server)
        session = server.app.state.session
        loop_push = lambda ops: httpx.post(  # noqa: E731
            server.base + "/harness/push", json={"ops": ops}
        )
        session.state.scene = SceneState(
            id="pond", name="a quiet pond", env="forest", zones={"pond": 0.8}
        )
        ops = session.world.spawn_object("frog", zone="pond", object_id="frog")
        ops += session.world.spawn_object(
            "campfire", zone="center", object_id="fire", state="burning"
        )
        loop_push([op for op in ops if op["op"] == "object"])
        page.wait_for_function(
            "vrRoom.state().world.objects.filter(o => o.drawn).length === 2",
            timeout=8000,
        )
        world = page.evaluate("vrRoom.state().world")
        assert {o["id"] for o in world["objects"]} == {"frog", "fire"}
        page.wait_for_timeout(600)
        _shot(page, "world-objects.png")

        # Mika's magic: bolt flies, the frog vanishes on screen and in state.
        outcome = session.interactions.cast_on("mika", "frog")
        assert outcome.performed
        loop_push(
            [
                op
                for op in outcome.ops
                if op["op"] in ("world_fx", "world_sfx", "attention", "action")
            ]
        )
        page.wait_for_timeout(1500)
        _shot(page, "world-magic-bolt.png")
        page.wait_for_timeout(700)
        landed = session.tick()
        assert "frog" not in session.state.objects
        loop_push(
            [
                op
                for op in landed
                if op["op"]
                in ("object", "world_fx", "world_sfx", "action", "attention")
            ]
        )
        page.wait_for_function(
            "!vrRoom.state().world.objects.some(o => o.id === 'frog')", timeout=4000
        )
        page.wait_for_timeout(250)
        _shot(page, "world-poof.png")

        # A viewer asks Mika to walk to the center: a calm glide, she arrives.
        httpx.post(
            server.base + "/harness/chat",
            json={"user": "@selwyn", "text": "walk towards the center"},
        )
        page.wait_for_function(
            "vrRoom.state().stage.find(s => s.id === 'mika').move === 'walk'",
            timeout=4000,
        )
        page.wait_for_timeout(1200)
        _shot(page, "world-glide.png")
        page.wait_for_function(
            "vrRoom.state().stage.find(s => s.id === 'mika').move === null",
            timeout=10000,
        )
        mika = next(
            s for s in page.evaluate("vrRoom.state().stage") if s["id"] == "mika"
        )
        assert abs(mika["x"] - session.state.characters["mika"].x * 1920) < 4
        world = page.evaluate("vrRoom.state().world")
        assert world["particles"] <= 220 and world["errors"] == 0
        assert usage.snapshot()["llm_requests"] == 0
        assert not errors
