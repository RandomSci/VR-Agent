"""End-to-end check of one viewer message through the WebSocket handler.

The LLM/TTS pipeline is replaced by a fake that emits one audio payload, so
the test covers: capability lookup, intent parsing, prompt, overlay card,
action message, state transitions, latency tracking and the livestream
client preference.
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

handler_mod = pytest.importorskip("open_llm_vtuber.websocket_handler")

from open_llm_vtuber.config_manager.live import VRAgentConfig  # noqa: E402
from open_llm_vtuber.live.youtube_live import YouTubeChatMessage  # noqa: E402
from open_llm_vtuber.vr_agent import runtime  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    def of_type(self, kind):
        return [m for m in self.sent if m.get("type") == kind]


def fake_context():
    model_info = json.loads((ROOT / "model_dict.json").read_text())[0]
    return SimpleNamespace(
        live2d_model=SimpleNamespace(model_info=model_info),
        config=SimpleNamespace(live_config=SimpleNamespace(vr_agent=VRAgentConfig())),
    )


def build_handler(monkeypatch, captured):
    async def fake_conversation(
        context, websocket_send, client_uid, user_input, images, metadata
    ):
        captured["prompt"] = user_input
        captured["metadata"] = metadata
        await asyncio.sleep(0.05)
        await websocket_send(
            json.dumps(
                {"type": "audio", "audio": None, "display_text": {"text": "Of course!"}}
            )
        )
        await asyncio.sleep(0.05)
        return "Of course!"

    monkeypatch.setattr(handler_mod, "process_single_conversation", fake_conversation)
    monkeypatch.chdir(ROOT)
    handler = handler_mod.WebSocketHandler(SimpleNamespace())
    return handler


def message(text, author="@Emil"):
    return YouTubeChatMessage(
        message_id=f"id-{time.time_ns()}",
        author_channel_id=f"name:{author}",
        author_display_name=author,
        text=text,
        timestamp=datetime.now(timezone.utc),
    )


def test_viewer_request_produces_card_action_prompt_and_states(monkeypatch):
    captured = {}
    handler = build_handler(monkeypatch, captured)
    dev, live = FakeSocket(), FakeSocket()
    handler.client_connections = {"dev": dev, "live": live}
    handler.client_contexts = {"dev": fake_context(), "live": fake_context()}

    async def scenario():
        await handler._handle_vr_agent_hello(
            live, "live", {"type": "vr-agent-hello", "mode": "live"}
        )
        ok = await handler.process_youtube_live_message(
            message("Can you clap?‮ <script>x</script>"), received_at=time.time() - 1.0
        )
        await asyncio.sleep(0.05)
        return ok

    assert asyncio.run(scenario()) is True

    # The livestream page gets the reply, not the dev page.
    assert dev.of_type("audio") == [] and len(live.of_type("audio")) == 1

    card_on, card_off = live.of_type("youtube-live-selected-message")
    assert card_on["active"] is True and card_off["active"] is False
    assert card_on["author"] == "@Emil"
    assert "‮" not in card_on["message"]

    (action,) = live.of_type("vr-agent-action")
    assert action == {
        "type": "vr-agent-action",
        "action": "nod",
        "sync": "speech",
        "id": card_on["id"],
    }

    prompt = captured["prompt"]
    assert "live on YouTube" in prompt and "clap" in prompt and "cannot do" in prompt
    assert "cheerful nod" in prompt

    phases = [m["phase"] for m in dev.of_type("vr-agent-state")]
    assert phases[:2] == ["thinking", "responding"] and phases[-1] == "listening"

    last = runtime.latency.summary()["last"]
    assert last["action"] == "nod" and last["chat_to_voice_s"] >= 1.0


def test_system_banter_shows_no_card_and_no_action(monkeypatch):
    captured = {}
    handler = build_handler(monkeypatch, captured)
    sock = FakeSocket()
    handler.client_connections = {"a": sock}
    handler.client_contexts = {"a": fake_context()}
    quiet = YouTubeChatMessage(
        message_id="idle-1",
        author_channel_id="system-idle",
        author_display_name="Chat",
        text="The live chat is quiet. Smile and say something.",
        timestamp=datetime.now(timezone.utc),
    )
    asyncio.run(handler.process_youtube_live_message(quiet))
    assert sock.of_type("youtube-live-selected-message") == []
    assert sock.of_type("vr-agent-action") == []
    assert captured["metadata"]["skip_history"] is True


def test_initial_config_payload_carries_capabilities(monkeypatch):
    handler = build_handler(monkeypatch, {})
    sock = FakeSocket()
    asyncio.run(handler._send_vr_agent_config(sock, fake_context()))
    (cfg,) = sock.of_type("vr-agent-config")
    actions = cfg["capabilities"]["actions"]
    assert actions["nod"] == {
        "name": "nod",
        "kind": "motion",
        "idle": True,
        "idle_weight": 3.0,
        "group": "",
        "index": 0,
        "duration": 3.47,
    }
    assert "clap" not in actions
    assert cfg["settings"]["idle_min_seconds"] == 10.0
