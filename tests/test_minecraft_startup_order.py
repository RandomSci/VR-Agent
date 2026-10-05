import asyncio
from types import SimpleNamespace as NS

from src.open_llm_vtuber.room import minecraft_mode as mm


class FakeEngine:
    def __init__(self, _runtimes, session):
        self.session = session
        self.camera_client_ready = asyncio.Event()
        self.camera_client_ok = False
        self.game_window_ready = asyncio.Event()
        self.game_window_ok = False
        self.started = False

    def start(self):
        self.started = True


def test_minecraft_mode_waits_for_graphical_client_before_obs(monkeypatch):
    async def run():
        session = NS(mode_engine=None)
        monkeypatch.setattr(mm, "minecraft_mode_enabled", lambda: True)
        monkeypatch.setattr(mm, "MinecraftEngine", FakeEngine)
        monkeypatch.setenv("MINECRAFT_OBS_START_WAIT_SECONDS", "30")

        task = asyncio.create_task(mm.prepare_minecraft_before_obs(session))
        await asyncio.sleep(0)
        assert session.mode_engine.started
        assert not task.done()
        session.mode_engine.game_window_ok = True
        session.mode_engine.game_window_ready.set()
        await task
        return session.mode_engine.game_window_ok

    assert asyncio.run(run()) is True


def test_non_minecraft_mode_keeps_existing_obs_startup(monkeypatch):
    async def run():
        session = NS(mode_engine=None)
        monkeypatch.setattr(mm, "minecraft_mode_enabled", lambda: False)
        await mm.prepare_minecraft_before_obs(session)
        return session.mode_engine

    assert asyncio.run(run()) is None


def test_minecraft_graphical_client_timeout_falls_back(monkeypatch):
    async def run():
        session = NS(mode_engine=None)
        monkeypatch.setattr(mm, "minecraft_mode_enabled", lambda: True)
        monkeypatch.setattr(mm, "MinecraftEngine", FakeEngine)
        monkeypatch.setenv("MINECRAFT_OBS_START_WAIT_SECONDS", "0.01")
        await mm.prepare_minecraft_before_obs(session)
        return session.mode_engine.started, session.mode_engine.game_window_ok

    assert asyncio.run(run()) == (True, False)


def test_failed_camera_client_attempt_does_not_release_obs_readiness(monkeypatch):
    async def run():
        session = NS(mode_engine=None)
        monkeypatch.setattr(mm, "minecraft_mode_enabled", lambda: True)
        monkeypatch.setattr(mm, "MinecraftEngine", FakeEngine)
        monkeypatch.setenv("MINECRAFT_OBS_START_WAIT_SECONDS", "30")
        task = asyncio.create_task(mm.prepare_minecraft_before_obs(session))
        await asyncio.sleep(0)
        session.mode_engine.camera_client_ok = False
        # Failed Multiplayer joining must not matter. OBS waits for game window only.
        session.mode_engine.game_window_ok = True
        session.mode_engine.game_window_ready.set()
        await asyncio.sleep(0)
        await task
        return session.mode_engine.game_window_ok, session.mode_engine.camera_client_ok

    assert asyncio.run(run()) == (True, False)


def test_launcher_window_alone_does_not_release_obs(monkeypatch):
    async def run():
        session = NS(mode_engine=None)
        monkeypatch.setattr(mm, "minecraft_mode_enabled", lambda: True)
        monkeypatch.setattr(mm, "MinecraftEngine", FakeEngine)
        monkeypatch.setenv("MINECRAFT_OBS_START_WAIT_SECONDS", "0.01")
        await mm.prepare_minecraft_before_obs(session)
        return session.mode_engine.game_window_ok

    assert asyncio.run(run()) is False


def test_startup_wait_cancels_cleanly(monkeypatch):
    async def run():
        session = NS(mode_engine=None)
        monkeypatch.setattr(mm, "minecraft_mode_enabled", lambda: True)
        monkeypatch.setattr(mm, "MinecraftEngine", FakeEngine)
        monkeypatch.setenv("MINECRAFT_OBS_START_WAIT_SECONDS", "30")
        task = asyncio.create_task(mm.prepare_minecraft_before_obs(session))
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False

    assert asyncio.run(run()) is True
