"""Minimal server for rendering the room page without ASR, LLM or TTS.

Serves the real frontend, Live2D models and backgrounds, and a /client-ws
endpoint backed by the real RoomSession. Used by the headless end-to-end
tests and handy for local layout work:

    uv run python -m tests.harness.room_server --port 12399

Extra developer endpoints:
    POST /harness/chat     {"user": "@ana", "text": "play trivia"}   a viewer message
    POST /harness/push     {"ops": [...]}              raw vr-room-update ops
    POST /harness/send     {...}                       any payload to room pages
    GET  /harness/clients  connected room pages and their model status
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from starlette.staticfiles import StaticFiles  # noqa: E402

from open_llm_vtuber.message_handler import message_handler  # noqa: E402
from open_llm_vtuber.room.live_message import LiveMessage  # noqa: E402
from open_llm_vtuber.room.profiles import load_room  # noqa: E402
from open_llm_vtuber.room.session import RoomSession  # noqa: E402
from tests.harness.fake_tts import FakeTTS  # noqa: E402


def create_app(room_dir: Path | None = None, tts=None) -> FastAPI:
    app = FastAPI()
    session = RoomSession(load_room(room_dir or ROOT / "room", ROOT))
    # Every voice uses the fake engine: no network, still counted as TTS usage.
    fake = tts or FakeTTS()
    session.voices.factory = lambda engine_type, **kwargs: fake
    session.configure_voices(None, fake)
    app.state.session = session
    app.state.tts = fake
    app.state.received = []  # messages from room pages, for tests

    @app.websocket("/client-ws")
    async def client_ws(websocket: WebSocket):
        await websocket.accept()
        uid = str(uuid.uuid4())
        try:
            while True:
                data = await websocket.receive_json()
                app.state.received.append({"uid": uid, **data})
                del app.state.received[:-500]
                message_handler.handle_message(uid, data)
                kind = data.get("type")
                if kind == "vr-agent-hello" and data.get("mode") == "room":
                    if session.active:
                        await session.register(uid, websocket.send_text)
                    else:
                        await websocket.send_text(
                            json.dumps(
                                {"type": "vr-room-config", "room": {"enabled": False}}
                            )
                        )
                elif kind == "vr-room-client-status":
                    session.on_client_status(
                        uid, data.get("loaded"), data.get("failed")
                    )
                elif kind == "heartbeat":
                    await websocket.send_text(json.dumps({"type": "heartbeat-ack"}))
        except WebSocketDisconnect:
            pass
        finally:
            session.unregister(uid)

    @app.post("/harness/chat")
    async def chat(request: Request):
        import time as _time

        body = await request.json()
        message = LiveMessage(
            platform="harness",
            message_id=f"h-{_time.time_ns()}",
            username=str(body.get("user") or "@viewer"),
            text=str(body.get("text") or ""),
            timestamp=_time.time(),
            author_id="harness-viewer",
        )
        consumed = session.observe_viewer_message(message)
        return JSONResponse({"consumed": consumed})

    @app.post("/harness/push")
    async def push(request: Request):
        body = await request.json()
        await session.push(body.get("ops") or [])
        return JSONResponse({"ok": True})

    @app.post("/harness/send")
    async def send(request: Request):
        await session.broadcast(await request.json())
        return JSONResponse({"ok": True})

    @app.post("/harness/play")
    async def play(request: Request):
        body = await request.json()
        ops = session.play(str(body.get("character")), str(body.get("action")))
        await session.push(ops)
        return JSONResponse({"ops": [o["op"] for o in ops]})

    @app.post("/harness/emit")
    async def emit(request: Request):
        body = await request.json()
        ops = await session.emit_and_push(
            str(body.get("name")), **(body.get("data") or {})
        )
        return JSONResponse({"ops": [o["op"] for o in ops]})

    @app.get("/harness/clients")
    async def clients():
        return JSONResponse(session.status())

    @app.get("/harness/received")
    async def received():
        return JSONResponse(app.state.received[-200:])

    app.mount(
        "/live2d-models", StaticFiles(directory=ROOT / "live2d-models"), name="live2d"
    )
    app.mount("/bg", StaticFiles(directory=ROOT / "backgrounds"), name="bg")
    app.mount("/", StaticFiles(directory=ROOT / "frontend", html=True), name="frontend")
    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=12399)
    args = parser.parse_args()
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    asyncio.run(asyncio.sleep(0))
    main()
