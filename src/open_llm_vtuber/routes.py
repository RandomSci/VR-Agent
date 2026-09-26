import os
import json
from uuid import uuid4
import numpy as np
from datetime import datetime
from fastapi import APIRouter, WebSocket, UploadFile, File, Response, Request
from starlette.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect
from loguru import logger
from .service_context import ServiceContext
from .websocket_handler import WebSocketHandler
from .proxy_handler import ProxyHandler


def init_client_ws_route(ws_handler: WebSocketHandler) -> APIRouter:
    """
    Create and return API routes for handling the `/client-ws` WebSocket connections.

    Args:
        default_context_cache: Default service context cache for new sessions.

    Returns:
        APIRouter: Configured router with WebSocket endpoint.
    """

    router = APIRouter()

    @router.websocket("/client-ws")
    async def websocket_endpoint(websocket: WebSocket):
        """WebSocket endpoint for client connections"""
        await websocket.accept()
        client_uid = str(uuid4())

        try:
            await ws_handler.handle_new_connection(websocket, client_uid)
            await ws_handler.handle_websocket_communication(websocket, client_uid)
        except WebSocketDisconnect:
            await ws_handler.handle_disconnect(client_uid)
        except Exception as e:
            logger.error(f"Error in WebSocket connection: {e}")
            await ws_handler.handle_disconnect(client_uid)
            raise

    return router


def init_proxy_route(server_url: str) -> APIRouter:
    """
    Create and return API routes for handling proxy connections.

    Args:
        server_url: The WebSocket URL of the actual server

    Returns:
        APIRouter: Configured router with proxy WebSocket endpoint
    """
    router = APIRouter()
    proxy_handler = ProxyHandler(server_url)

    @router.websocket("/proxy-ws")
    async def proxy_endpoint(websocket: WebSocket):
        """WebSocket endpoint for proxy connections"""
        try:
            await proxy_handler.handle_client_connection(websocket)
        except Exception as e:
            logger.error(f"Error in proxy connection: {e}")
            raise

    return router


def init_webtool_routes(
    default_context_cache: ServiceContext,
    youtube_live_service_getter=None,
    ws_handler: WebSocketHandler | None = None,
) -> APIRouter:
    """
    Create and return API routes for handling web tool interactions.

    Args:
        default_context_cache: Default service context cache for new sessions.

    Returns:
        APIRouter: Configured router with WebSocket endpoint.
    """

    router = APIRouter()

    def get_youtube_live_service():
        if youtube_live_service_getter:
            return youtube_live_service_getter()
        return None

    @router.get("/youtube-live/status")
    async def youtube_live_status():
        youtube_live_service = get_youtube_live_service()
        if not youtube_live_service:
            return JSONResponse({"enabled": False, "running": False})
        return JSONResponse(youtube_live_service.status())

    @router.get("/vr-agent/status")
    async def vr_agent_status():
        """Developer monitoring: state, latency, capabilities, chat reader health."""
        from .vr_agent import runtime
        from .vr_agent.usage import usage

        youtube_live_service = get_youtube_live_service()
        caps = (
            ws_handler.get_capabilities(default_context_cache) if ws_handler else None
        )
        return JSONResponse(
            {
                "state": runtime.snapshot(),
                "latency": runtime.latency.summary(),
                "connected_clients": len(ws_handler.client_connections)
                if ws_handler
                else 0,
                "livestream_clients": len(ws_handler.live_client_uids)
                if ws_handler
                else 0,
                "capabilities": caps.describe() if caps else None,
                "youtube": youtube_live_service.status()
                if youtube_live_service
                else None,
                "room": ws_handler.room_session.status() if ws_handler else None,
                "usage": usage.snapshot(),
            }
        )

    @router.post("/vr-agent/pause")
    async def vr_agent_pause(request: Request):
        """Be-right-back mode. Body {"paused": true|false}; no body toggles."""
        from .vr_agent import runtime

        try:
            payload = await request.json()
        except Exception:
            payload = {}
        paused = payload.get("paused") if isinstance(payload, dict) else None
        runtime.set_paused(not runtime.paused if paused is None else bool(paused))
        return JSONResponse({"paused": runtime.paused})

    @router.get("/vr-agent/health")
    async def vr_agent_health():
        """Tiny liveness check for the OBS be-right-back watchdog."""
        from .vr_agent import runtime

        return JSONResponse(
            {
                "ok": True,
                "paused": runtime.paused,
                # Classic live pages and room pages both count as the stream page.
                "livestream_clients": len(ws_handler.live_client_uids)
                + len(ws_handler.room_client_uids)
                if ws_handler
                else 0,
            }
        )

    @router.post("/vr-agent/test-action")
    async def vr_agent_test_action(request: Request):
        """Developer tool: play a registry action on the livestream page."""
        if not ws_handler:
            return JSONResponse({"error": "unavailable"}, status_code=503)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        name = str(payload.get("action", ""))
        primary = ws_handler._get_primary_client()
        if not primary:
            return JSONResponse({"error": "no connected frontend"}, status_code=409)
        client_uid, websocket, context = primary
        caps = ws_handler.get_capabilities(context)
        if not caps or not caps.get(name):
            available = sorted(caps.actions) if caps else []
            return JSONResponse(
                {"error": f"unknown action '{name}'", "available": available},
                status_code=400,
            )
        await websocket.send_text(
            json.dumps({"type": "vr-agent-action", "action": name, "sync": "now"})
        )
        return JSONResponse({"sent": name, "client": client_uid})

    @router.get("/vr-agent/latency")
    async def vr_agent_latency():
        """Developer monitoring: per-stage latency from chat message to first audible speech."""
        from .vr_agent.latency_trace import tracker

        return JSONResponse({"summary": tracker.summary(), "recent": tracker.recent(10)})

    @router.get("/vr-agent/room/status")
    async def vr_room_status():
        """Developer monitoring for the multi-character room."""
        if not ws_handler:
            return JSONResponse({"error": "unavailable"}, status_code=503)
        return JSONResponse(ws_handler.room_session.status())

    @router.post("/vr-agent/room/attention")
    async def vr_room_attention(request: Request):
        """Developer tool. Body {"character": "mika", "target": "CHARACTER:luna", "hold_seconds": 4}."""
        if not ws_handler or not ws_handler.room_session.has_clients():
            return JSONResponse({"error": "no room page connected"}, status_code=409)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        try:
            op = ws_handler.room_session.attention_op(
                str(payload.get("character", "")),
                str(payload.get("target", "")),
                source="developer",
                hold_seconds=float(payload.get("hold_seconds", 4)),
            )
        except (TypeError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if not op:
            return JSONResponse({"error": "unknown character or target"}, status_code=400)
        await ws_handler.room_session.push([op])
        return JSONResponse({"sent": op})

    @router.post("/vr-agent/room/test-action")
    async def vr_room_test_action(request: Request):
        """Developer tool. Body {"character": "luna", "action": "cheer"}."""
        if not ws_handler or not ws_handler.room_session.has_clients():
            return JSONResponse({"error": "no room page connected"}, status_code=409)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        ops = ws_handler.room_session.play(
            str(payload.get("character", "")), str(payload.get("action", ""))
        )
        if not ops:
            return JSONResponse({"error": "unknown character or action"}, status_code=400)
        await ws_handler.room_session.push(ops)
        return JSONResponse({"sent": [o.get("op") for o in ops]})

    @router.post("/vr-agent/room/event")
    async def vr_room_event(request: Request):
        """Developer tool: emit a room event, e.g. {"name": "QUESTION_SHOWN"}.

        Only known event names are accepted and data is limited to short
        scalars, so this cannot inject arbitrary ops.
        """
        from .room.events import ALL_EVENTS

        if not ws_handler:
            return JSONResponse({"error": "unavailable"}, status_code=503)
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        name = str(payload.get("name", ""))
        if name not in ALL_EVENTS:
            return JSONResponse({"error": "unknown event", "known": sorted(ALL_EVENTS)}, status_code=400)
        data = {}
        for key, value in (payload.get("data") or {}).items():
            if isinstance(key, str) and key.isidentifier() and len(key) <= 24:
                if isinstance(value, (bool, int, float)) or value is None:
                    data[key] = value
                elif isinstance(value, str):
                    data[key] = value[:60]
                elif isinstance(value, list):
                    data[key] = [str(v)[:24] for v in value[:6]]
        ops = await ws_handler.room_session.emit_and_push(name, **data)
        return JSONResponse({"event": name, "ops": [o.get("op") for o in ops]})

    @router.post("/youtube-live/mock-message")
    async def youtube_live_mock_message(request: Request):
        youtube_live_service = get_youtube_live_service()
        if not youtube_live_service:
            return JSONResponse(
                {"error": "YouTube Live service is unavailable"}, status_code=503
            )
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        author = str(payload.get("author", "Mock Viewer"))
        message = str(payload.get("message", "")).strip()
        if not message:
            return JSONResponse({"error": "message is required"}, status_code=400)
        result = await youtube_live_service.inject_mock_message(author, message)
        return JSONResponse(result)

    @router.get("/web-tool")
    async def web_tool_redirect():
        """Redirect /web-tool to /web_tool/index.html"""
        return Response(status_code=302, headers={"Location": "/web-tool/index.html"})

    @router.get("/web_tool")
    async def web_tool_redirect_alt():
        """Redirect /web_tool to /web_tool/index.html"""
        return Response(status_code=302, headers={"Location": "/web-tool/index.html"})

    @router.get("/live2d-models/info")
    async def get_live2d_folder_info():
        """Get information about available Live2D models"""
        live2d_dir = "live2d-models"
        if not os.path.exists(live2d_dir):
            return JSONResponse(
                {"error": "Live2D models directory not found"}, status_code=404
            )

        valid_characters = []
        supported_extensions = [".png", ".jpg", ".jpeg"]

        for entry in os.scandir(live2d_dir):
            if entry.is_dir():
                folder_name = entry.name.replace("\\", "/")
                model3_file = os.path.join(
                    live2d_dir, folder_name, f"{folder_name}.model3.json"
                ).replace("\\", "/")

                if os.path.isfile(model3_file):
                    # Find avatar file if it exists
                    avatar_file = None
                    for ext in supported_extensions:
                        avatar_path = os.path.join(
                            live2d_dir, folder_name, f"{folder_name}{ext}"
                        )
                        if os.path.isfile(avatar_path):
                            avatar_file = avatar_path.replace("\\", "/")
                            break

                    valid_characters.append(
                        {
                            "name": folder_name,
                            "avatar": avatar_file,
                            "model_path": model3_file,
                        }
                    )
        return JSONResponse(
            {
                "type": "live2d-models/info",
                "count": len(valid_characters),
                "characters": valid_characters,
            }
        )

    @router.post("/asr")
    async def transcribe_audio(file: UploadFile = File(...)):
        """
        Endpoint for transcribing audio using the ASR engine
        """
        logger.info(f"Received audio file for transcription: {file.filename}")

        try:
            contents = await file.read()

            # Validate minimum file size
            if len(contents) < 44:  # Minimum WAV header size
                raise ValueError("Invalid WAV file: File too small")

            # Decode the WAV header and get actual audio data
            wav_header_size = 44  # Standard WAV header size
            audio_data = contents[wav_header_size:]

            # Validate audio data size
            if len(audio_data) % 2 != 0:
                raise ValueError("Invalid audio data: Buffer size must be even")

            # Convert to 16-bit PCM samples to float32
            try:
                audio_array = (
                    np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
                    / 32768.0
                )
            except ValueError as e:
                raise ValueError(
                    f"Audio format error: {str(e)}. Please ensure the file is 16-bit PCM WAV format."
                )

            # Validate audio data
            if len(audio_array) == 0:
                raise ValueError("Empty audio data")

            text = await default_context_cache.asr_engine.async_transcribe_np(
                audio_array
            )
            logger.info(f"Transcription result: {text}")
            return {"text": text}

        except ValueError as e:
            logger.error(f"Audio format error: {e}")
            return Response(
                content=json.dumps({"error": str(e)}),
                status_code=400,
                media_type="application/json",
            )
        except Exception as e:
            logger.error(f"Error during transcription: {e}")
            return Response(
                content=json.dumps(
                    {"error": "Internal server error during transcription"}
                ),
                status_code=500,
                media_type="application/json",
            )

    @router.websocket("/tts-ws")
    async def tts_endpoint(websocket: WebSocket):
        """WebSocket endpoint for TTS generation"""
        await websocket.accept()
        logger.info("TTS WebSocket connection established")

        try:
            while True:
                data = await websocket.receive_json()
                text = data.get("text")
                if not text:
                    continue

                logger.info(f"Received text for TTS: {text}")

                # Split text into sentences
                sentences = [s.strip() for s in text.split(".") if s.strip()]

                try:
                    # Generate and send audio for each sentence
                    for sentence in sentences:
                        sentence = sentence + "."  # Add back the period
                        file_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{str(uuid4())[:8]}"
                        from .vr_agent.usage import usage

                        usage.record_tts("tts_web_tool")
                        audio_path = (
                            await default_context_cache.tts_engine.async_generate_audio(
                                text=sentence, file_name_no_ext=file_name
                            )
                        )
                        logger.info(
                            f"Generated audio for sentence: {sentence} at: {audio_path}"
                        )

                        await websocket.send_json(
                            {
                                "status": "partial",
                                "audioPath": audio_path,
                                "text": sentence,
                            }
                        )

                    # Send completion signal
                    await websocket.send_json({"status": "complete"})

                except Exception as e:
                    logger.error(f"Error generating TTS: {e}")
                    await websocket.send_json({"status": "error", "message": str(e)})

        except WebSocketDisconnect:
            logger.info("TTS WebSocket client disconnected")
        except Exception as e:
            logger.error(f"Error in TTS WebSocket connection: {e}")
            await websocket.close()

    return router
