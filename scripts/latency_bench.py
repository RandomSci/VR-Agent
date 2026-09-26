"""Latency benchmark for the room pipeline with SIMULATED OpenAI and TTS timing.

Runs the real code path end to end: YouTube chat service (filter, buffer,
selection, cooldown), WebSocket handler, Conversation Director routing, the
real BasicMemoryAgent with the real sentence splitter, the real TTS manager,
real audio encoding (pydub), and a fake room page that reports playback.

Only the network services are simulated:
  * LLM: first token after --llm-first-token seconds, then --llm-tps tokens/s
  * TTS: --tts-base seconds plus --tts-per-char seconds per character

The numbers therefore show our own pipeline overhead plus the simulated API
time. They are NOT measurements of real OpenAI or ElevenLabs latency. Use
GET /vr-agent/latency on the real server for that.

    uv run python scripts/latency_bench.py --scenario single
    uv run python scripts/latency_bench.py --scenario back_to_back --cooldown 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import struct
import sys
import tempfile
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from loguru import logger  # noqa: E402

logger.remove()
logger.add(sys.stderr, level="WARNING")

REPLIES = {
    "normal": (
        "Oh, stars are my favorite thing to look at on quiet nights. "
        "Orion is the easiest one to find, even from the city."
    ),
    "short": "Hi there, welcome in!",
}
REPLY = REPLIES["normal"]


class SimLLM:
    """Streams REPLY word by word like a chat completion stream."""

    def __init__(self, first_token: float, tps: float):
        self.first_token = first_token
        self.tps = tps

    async def chat_completion(self, messages, system=None, tools=None):
        await asyncio.sleep(self.first_token)
        words = REPLY.split(" ")
        for i, word in enumerate(words):
            yield word if i == 0 else " " + word
            await asyncio.sleep(1.3 / self.tps)  # about 1.3 tokens per word


class SimTTS:
    """Blocking synthesis in a worker thread, like the ElevenLabs engine."""

    def __init__(self, base: float, per_char: float):
        self.base = base
        self.per_char = per_char
        self.calls: list[str] = []

    async def async_generate_audio(self, text, file_name_no_ext=None):
        return await asyncio.to_thread(self.generate_audio, text, file_name_no_ext)

    def generate_audio(self, text, file_name_no_ext=None):
        from open_llm_vtuber.vr_agent import latency_trace

        self.calls.append(text)
        time.sleep(self.base * 0.6)
        latency_trace.mark("tts_first_byte")
        time.sleep(self.base * 0.4 + self.per_char * len(text))
        seconds = 0.3 + len(text) * 0.06
        rate = 22050
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="vr-bench-")
        os.close(fd)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(
                b"".join(
                    struct.pack("<h", int(6000 * math.sin(i / 8.0)))
                    for i in range(int(rate * seconds))
                )
            )
        return path

    def remove_file(self, path, verbose=True):
        try:
            os.remove(path)
        except OSError:
            pass


class RoomPage:
    """Fake room.js: plays audio instantly, reports start and completion."""

    def __init__(self, handler, uid: str, play_delay: float):
        self.handler = handler
        self.uid = uid
        self.play_delay = play_delay
        self.sent: list[dict] = []
        self.playing_until = 0.0

    async def send_text(self, text: str) -> None:
        from open_llm_vtuber.message_handler import message_handler

        payload = json.loads(text)
        self.sent.append(payload)
        loop = asyncio.get_running_loop()
        if payload.get("type") == "audio" and payload.get("audio"):

            async def started():
                await asyncio.sleep(self.play_delay)
                await self.handler._handle_audio_play_start(
                    self,
                    self.uid,
                    {"type": "audio-play-start", "client_time": time.time() * 1000},
                )

            loop.create_task(started())
            duration = len(payload.get("volumes") or []) * 0.02
            self.playing_until = max(self.playing_until, time.time()) + duration
        if payload.get("type") == "backend-synth-complete":
            delay = max(0.0, self.playing_until - time.time())
            loop.call_later(
                delay,
                message_handler.handle_message,
                self.uid,
                {"type": "frontend-playback-complete"},
            )


def base_context(sim_tts):
    from open_llm_vtuber.config_manager import read_yaml, validate_config

    raw = read_yaml("config_templates/conf.default.yaml")
    raw["character_config"]["agent_config"]["agent_settings"]["basic_memory_agent"][
        "llm_provider"
    ] = "openai_compatible_llm"
    cfg = validate_config(raw)
    return SimpleNamespace(
        character_config=cfg.character_config,
        system_config=cfg.system_config,
        tts_engine=sim_tts,
    )


async def run(args) -> dict:
    from open_llm_vtuber import websocket_handler as handler_mod
    from open_llm_vtuber.agent import agent_factory
    from open_llm_vtuber.live.youtube_live import (
        YouTubeChatMessage,
        YouTubeLiveChatService,
    )
    from open_llm_vtuber.vr_agent import latency_trace
    from open_llm_vtuber.vr_agent.state import runtime

    runtime.paused = False
    sim_llm = SimLLM(args.llm_first_token, args.llm_tps)
    sim_tts = SimTTS(args.tts_base, args.tts_per_char)
    agent_factory.StatelessLLMFactory.create_llm = staticmethod(lambda **kw: sim_llm)

    ctx = base_context(sim_tts)
    handler = handler_mod.WebSocketHandler(ctx)
    handler.room_session.voices.factory = lambda engine_type, **kw: sim_tts
    handler.room_session.configure_voices(None, sim_tts)
    page = RoomPage(handler, "room", args.play_delay)
    handler.client_connections = {"room": page}
    handler.client_contexts = {"room": SimpleNamespace()}
    await handler._handle_vr_agent_hello(
        page, "room", {"type": "vr-agent-hello", "mode": "room"}
    )
    handler.room_session.on_client_status("room", ["mika", "luna"], [])
    if handler.room_session._loop_task:
        handler.room_session._loop_task.cancel()

    config = SimpleNamespace(
        youtube_live_enabled=True,
        chat_source="api",
        api_key="",
        channel_id="",
        video_id=None,
        prefer_stream_list=True,
        max_buffer_messages=100,
        message_buffer_seconds=180,
        same_user_cooldown_seconds=0,
        response_cooldown_seconds=args.cooldown,
        selector_max_messages=12,
        idle_banter_enabled=False,
        idle_banter_delay_seconds=0,
        discovery_retry_seconds=10,
        single_message_fast_path=not args.no_fast_path,
    )
    service = YouTubeLiveChatService(config, None, handler)
    handler.register_chat_service(service)
    service._running = True
    loop_task = asyncio.create_task(service._response_loop())

    def send(text: str, n: int) -> str:
        now = time.time()
        msg = YouTubeChatMessage(
            message_id=f"bench-{n}-{time.time_ns()}",
            author_channel_id=f"UCviewer{n}",
            author_display_name=f"@viewer{n}",
            text=text,
            timestamp=datetime.now(timezone.utc),
            dom_at=now - 0.045,  # observer batch delay (40 ms) plus binding hop
            detected_at=now,
        )
        service._accept(msg, "message")
        return msg.message_id

    async def wait_done(key: str, timeout: float = 30) -> None:
        end = time.time() + timeout
        while time.time() < end:
            trace = latency_trace.tracker.get(key)
            if trace and "interaction_finished" in trace.marks:
                return
            await asyncio.sleep(0.02)
        raise TimeoutError(key)

    results = []
    if args.scenario == "single":
        for i in range(args.repeat):
            key = send(f"Luna, what's your favorite star number {i}?", i)
            await wait_done(key)
            await asyncio.sleep(max(args.cooldown, 1) + 0.5)  # isolated messages
            results.append(latency_trace.tracker.get(key).report())
    else:  # back_to_back: next message arrives shortly after the previous reply ended
        for i in range(args.repeat):
            key = send(f"Luna, what's your favorite star number {i}?", i)
            await wait_done(key)
            await asyncio.sleep(args.gap)
            results.append(latency_trace.tracker.get(key).report())
        key = send("Mika, and yours?", 99)
        await wait_done(key)
        results.append(latency_trace.tracker.get(key).report())
    loop_task.cancel()
    return {
        "scenario": args.scenario,
        "assumptions": {
            "llm_first_token_s": args.llm_first_token,
            "llm_tokens_per_s": args.llm_tps,
            "tts_base_s": args.tts_base,
            "tts_per_char_s": args.tts_per_char,
            "response_cooldown_s": args.cooldown,
        },
        "results": results,
        "first_tts_chunk": sim_tts.calls[0] if sim_tts.calls else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--scenario", choices=["single", "back_to_back"], default="single"
    )
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--llm-first-token", type=float, default=0.7)
    parser.add_argument("--llm-tps", type=float, default=60.0)
    parser.add_argument("--tts-base", type=float, default=0.9)
    parser.add_argument("--tts-per-char", type=float, default=0.012)
    parser.add_argument("--cooldown", type=float, default=1.0)
    parser.add_argument("--gap", type=float, default=1.5)
    parser.add_argument("--play-delay", type=float, default=0.01)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--reply", choices=list(REPLIES), default="normal")
    parser.add_argument("--no-fast-path", action="store_true")
    args = parser.parse_args()
    global REPLY
    REPLY = REPLIES[args.reply]
    out = asyncio.run(run(args))
    if args.json:
        print(json.dumps(out, indent=1))
        return
    print(f"scenario {out['scenario']}  assumptions {out['assumptions']}")
    print(f"first TTS chunk: {out['first_tts_chunk']!r}")
    for r in out["results"]:
        spans = r["spans_ms"]
        print(
            "  total detected->audible {:>7} ms | queue+select {:>6} | routing {:>5} | llm first token {:>6} | "
            "first chunk {:>6} | tts first audio {:>6} | prepare+send {:>5} | playback {:>4}".format(
                spans["total_detected_to_audible"],
                spans["queue_and_selection"],
                spans["character_routing"],
                spans["llm_first_token"],
                spans["first_speakable_chunk"],
                spans["tts_first_audio"],
                spans["audio_prepare_and_send"],
                spans["playback_scheduling"],
            )
        )


if __name__ == "__main__":
    main()
