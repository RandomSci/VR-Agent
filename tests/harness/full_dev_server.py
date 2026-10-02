"""DEV server for Code in Public: the real room, teaching and coding pipeline.

    uv run python -m tests.harness.full_dev_server          # port 12399

This is the development counterpart of the livestream server. It serves the
real frontend (the Stage, Live2D models, backgrounds) and runs the real
RoomSession, ConversationDirector, TeachingDirector and coding runner. The one
substitution is ASR: it is not loaded, viewer messages arrive as text.

Voices are the real ones, built the same way as the livestream: Mika inherits
conf.yaml's TTS (Edge TTS, Ana) and Luna builds her own from luna.yaml (Edge
TTS, Maisie). Edge TTS is free, so this costs nothing.

    --fake-voices   speak with a test tone instead (offline, no network)

The automated tests always pass a fake engine, so they never touch the network.

The LLM is the real one from conf.yaml, so Mika and Luna behave here exactly
as they do on stream. With no API key configured the server still boots and
serves the Stage, and says so in the log.

Developer endpoints:
    POST /harness/chat     {"user": "@selwyn", "text": "mika how are you?"}
    GET  /harness/state    room, teaching and coding state in one place
    GET  /harness/clients  connected pages, usage and traces
    POST /harness/push     {"ops": [...]}   raw ops to the pages
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, Response  # noqa: E402
from loguru import logger  # noqa: E402
from starlette.staticfiles import StaticFiles  # noqa: E402

from open_llm_vtuber.message_handler import message_handler  # noqa: E402
from open_llm_vtuber.room.capabilities import install_stage_headers  # noqa: E402
from open_llm_vtuber.room.live_message import LiveMessage  # noqa: E402
from open_llm_vtuber.room.profiles import load_room  # noqa: E402
from open_llm_vtuber.room.runtime import RoomRuntimes  # noqa: E402
from open_llm_vtuber.room.session import RoomSession  # noqa: E402
from open_llm_vtuber.vr_agent.text_safety import clean_viewer_text  # noqa: E402
from tests.harness.dev_context import (  # noqa: E402
    build_conf_tts,
    describe_llm,
    describe_tts,
    has_llm_key,
    load_dev_context,
)
from tests.harness.fake_tts import FakeTTS  # noqa: E402

DEFAULT_PORT = 12399
STAGE_PAGE = "vr-agent/teaching-stage.html"

# Added to the Stage page by this server (the page file is never edited).
# It reports whether speech audio really played, and when the browser blocks
# sound it shows one big "click once" banner instead of failing silently.
SOUND_GUARD_JS = r"""
(function () {
  if (window.__vrSoundGuard) return;
  window.__vrSoundGuard = true;
  var lastSent = "";
  function report(state, detail) {
    var key = state + ":" + (detail || "");
    if (key === lastSent && state === "playing") return;
    lastSent = key;
    try {
      fetch("/harness/sound", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ state: state, detail: detail || "" }) });
    } catch (_) {}
  }
  var banner = null;
  function showBanner() {
    if (banner || !document.body) return;
    banner = document.createElement("div");
    banner.textContent = "Sound is blocked by the browser. Click anywhere once to hear Mika and Luna.";
    banner.style.cssText = "position:fixed;left:50%;top:40%;transform:translate(-50%,-50%);z-index:2147483647;" +
      "padding:28px 40px;border-radius:18px;background:#ffb55e;color:#0f1d3a;font:800 30px/1.3 system-ui,sans-serif;" +
      "box-shadow:0 0 0 6px rgba(255,181,94,.35),0 20px 60px rgba(0,0,0,.5);text-align:center;max-width:80vw;cursor:pointer";
    document.body.appendChild(banner);
  }
  function unlock() {
    if (banner) { banner.remove(); banner = null; }
    try {
      var a = new Audio("data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA=");
      a.volume = 0;
      var p = a.play();
      if (p && p.then) p.then(function () { report("unlocked"); }, function () {});
    } catch (_) {}
  }
  ["pointerdown", "keydown", "touchstart"].forEach(function (evt) {
    window.addEventListener(evt, unlock, { capture: true, passive: true });
  });
  var play = HTMLMediaElement.prototype.play;
  HTMLMediaElement.prototype.play = function () {
    var media = this;
    var result = play.apply(this, arguments);
    if (media.volume === 0 || media.muted) return result;
    if (result && result.then) {
      result.then(
        function () { report("playing"); },
        function (err) {
          var name = (err && err.name) || "error";
          report(name === "NotAllowedError" ? "blocked" : "failed", name + " " + ((err && err.message) || ""));
          if (name === "NotAllowedError") showBanner();
        }
      );
    }
    return result;
  };
  report("page-loaded", navigator.userAgent);
})();
"""


class VoiceMeter:
    """Wraps a real TTS engine and records every line it makes.

    Lets the chat console prove the voice worked (or say exactly why not)
    instead of guessing from a silent stage.
    """

    def __init__(self, inner, label: str, events: list):
        self.primary = inner
        self._label = label
        self._events = events

    def _record(self, text: str, started: float, path, error: str = "") -> None:
        import os

        size = 0
        try:
            size = os.path.getsize(str(path)) if path else 0
        except OSError:
            size = 0
        source = str(self.__dict__.get("_vr_usage_source") or self._label)
        self._events.append(
            {
                "voice": source.replace("room:", ""),
                "engine_voice": getattr(self.primary, "voice", "")
                or getattr(self.primary, "voice_id", ""),
                "ok": bool(size),
                "bytes": size,
                "ms": int((time.perf_counter() - started) * 1000),
                "error": error[:200],
                "chars": len(text or ""),
                "at": time.time(),
            }
        )
        del self._events[:-200]

    async def async_generate_audio(self, text: str, file_name_no_ext=None):
        started = time.perf_counter()
        try:
            path = await self.primary.async_generate_audio(text, file_name_no_ext)
        except Exception as exc:
            self._record(text, started, None, str(exc))
            raise
        self._record(text, started, path, "" if path else "the voice returned no audio")
        return path

    def generate_audio(self, text: str, file_name_no_ext=None):
        started = time.perf_counter()
        try:
            path = self.primary.generate_audio(text, file_name_no_ext)
        except Exception as exc:
            self._record(text, started, None, str(exc))
            raise
        self._record(text, started, path, "" if path else "the voice returned no audio")
        return path

    def remove_file(self, filepath: str, verbose: bool = True) -> None:
        self.primary.remove_file(filepath, verbose)

    def __getattr__(self, name: str):
        return getattr(self.primary, name)


def create_app(
    root: Path | None = None, context=None, tts=None, check_base_url: str | None = None
) -> FastAPI:
    """``tts`` given: every character speaks with it (tests, --fake-voices).
    ``tts`` omitted: the real per-character voices, exactly like the livestream.
    """
    root = Path(root or ROOT)
    app = FastAPI()
    install_stage_headers(app)
    session = RoomSession(load_room(root / "room", root))
    # .env first, so ${OPENAI_API_KEY} in conf.yaml is filled from it. Values
    # already set in the real environment still win.
    from open_llm_vtuber.publishing.settings import load_dotenv

    load_dotenv(root / ".env")
    base_context = context if context is not None else load_dev_context(root)
    voice_events: list = []

    if tts is not None:
        # One engine for everyone. Only for tests and --fake-voices.
        session.voices.factory = lambda engine_type, **kwargs: tts
        session.configure_voices(None, tts)
        logger.warning("DEV voices: test tone (--fake-voices), not the real voices")
    else:
        # The livestream path: CharacterVoices keeps its real TTSFactory, so a
        # character with its own voice (Luna) builds it, and one that inherits
        # (Mika) speaks with conf.yaml's engine.
        from open_llm_vtuber.room.speech import _default_factory

        def metered_factory(engine_type, **kwargs):
            inner = _default_factory(engine_type, **kwargs)
            return (
                VoiceMeter(inner, engine_type, voice_events)
                if inner is not None
                else None
            )

        session.voices.factory = metered_factory
        try:
            tts_config, engine = build_conf_tts(base_context)
            inheritors = [
                c.id for c in session.room.characters if not c.voice.tts_model
            ]
            engine = VoiceMeter(
                engine, ",".join(inheritors) or "conf.yaml", voice_events
            )
            session.configure_voices(tts_config, engine)
            logger.info(f"DEV conf.yaml voice: {describe_tts(tts_config)}")
        except Exception as exc:
            session.configure_voices(None, None)
            logger.error(
                f"DEV: conf.yaml voice unavailable ({exc}). Characters that inherit it "
                "will not speak. Use --fake-voices to test without it."
            )
        for character in session.room.characters:
            voice = session.voices.engine(character.id)
            inner = getattr(voice, "primary", voice)
            logger.info(
                f"DEV voice for {character.id}: "
                f"{type(inner).__module__.rsplit('.', 1)[-1] if inner else 'none'} "
                f"{getattr(inner, 'voice', '') or getattr(inner, 'voice_id', '') or ''}".rstrip()
            )

    app.state.session = session
    app.state.context = base_context
    app.state.tts = tts
    app.state.voice_events = voice_events
    # Trusted publishing service. Every flag is off unless set in .env; with
    # PUBLISHING_DRY_RUN (the default) nothing leaves this machine.
    from open_llm_vtuber.publishing import PublicationService, PublishSettings

    publish_settings = PublishSettings.from_env()
    app.state.publisher = PublicationService(publish_settings)
    logger.info(
        "DEV publishing: "
        + (
            "off"
            if not publish_settings.enabled
            else ("dry run" if publish_settings.dry_run else "LIVE to GitHub")
        )
    )
    # Real-browser check of every new web program (one warm headless browser).
    app.state.browser_qa = None
    if check_base_url:
        from open_llm_vtuber.room.browser_qa import BrowserQA

        app.state.browser_qa = BrowserQA(check_base_url)
        logger.info(
            "DEV: web programs are checked in a real browser before they count as done"
        )

        @app.on_event("shutdown")
        async def close_browser_qa():
            await app.state.browser_qa.close()

    app.state.sound = {"state": "no stage page yet", "detail": "", "at": 0.0}
    app.state.runtimes = None
    app.state.received = []
    app.state.busy = asyncio.Lock()

    if session.room.problems:
        logger.warning(f"DEV room problems: {session.room.problems}")
    logger.info(f"DEV cast: {[c.id for c in session.room.characters]}")
    logger.info(f"DEV LLM: {describe_llm(base_context)}")
    if not has_llm_key(base_context):
        logger.warning(
            "DEV: no LLM API key configured, so Mika and Luna will not speak. "
            "The Stage still loads. Set the key in conf.yaml to talk to them."
        )

    def ensure_runtimes(client_uid: str, send) -> bool:
        """Wire the real pipeline, exactly like the livestream handler does."""
        runtimes = app.state.runtimes
        if runtimes is None:
            if base_context is None:
                return False
            runtimes = RoomRuntimes(session, base_context, client_uid, send)
            app.state.runtimes = runtimes
            session.director.turn_runner = runtimes.run_turn
            session.director.teaching_intent_classifier = (
                runtimes.classify_teaching_intent
            )
            session.director.coding_action_runner = runtimes.run_coding_action
            if app.state.browser_qa is not None:
                runtimes.browser_check = app.state.browser_qa.check
            runtimes.publisher = app.state.publisher
        else:
            runtimes.retarget(client_uid, send)
        return session.director.ready

    app.state.ensure_runtimes = ensure_runtimes

    # ------------------------------------------------------------------
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
                if kind == "vr-agent-hello":
                    if session.active:
                        await session.register(uid, websocket.send_text)
                        ensure_runtimes(uid, websocket.send_text)
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
        except Exception as exc:  # a broken page never takes the server down
            logger.warning(f"DEV: page {uid[:8]} dropped: {exc}")
        finally:
            session.unregister(uid)

    # ------------------------------------------------------------------
    async def deliver(user: str, text: str) -> dict:
        """One viewer comment through the whole real path."""
        message = LiveMessage(
            platform="dev",
            message_id=f"dev-{time.time_ns()}",
            username=user or "@viewer",
            text=text,
            timestamp=time.time(),
            author_id=f"dev:{(user or 'viewer').lstrip('@').lower()}",
        )
        if session.observe_viewer_message(message):
            return {"accepted": True, "consumed_by": "room", "spoke": []}
        if not session.director.ready:
            return {"accepted": False, "reason": "director not ready (no LLM?)"}
        started = time.perf_counter()
        started_wall = time.time()
        voice_mark = len(app.state.voice_events)
        if app.state.runtimes is not None:
            app.state.runtimes.last_decision_seconds = 0.0
            app.state.runtimes.last_generation_seconds = 0.0
        plan = session.director.plan(message)
        if not plan.turns:
            return {"accepted": False, "reason": plan.decision.reason}
        # The comment card, exactly like LIVE sends it, so the viewer's
        # message is on screen while Mika and Luna answer it.
        card = {
            "type": "youtube-live-selected-message",
            "active": True,
            "id": message.message_id,
            "author": clean_viewer_text(message.display_name, 60) or "Viewer",
            "message": clean_viewer_text(message.text, 180),
            "paid": False,
            "amount": "",
            "to": [t.speaker for t in plan.turns[:2]],
        }
        async with app.state.busy:
            await session.broadcast(card)
            session.begin_conversation()
            try:
                plan = await session.director.run(plan)
            finally:
                session.end_conversation()
                await session.broadcast(
                    {
                        "type": "youtube-live-selected-message",
                        "active": False,
                        "id": message.message_id,
                    }
                )
        runtimes = app.state.runtimes
        # Automatic publishing starts after a passing build: wait for it, so
        # the terminal can show the link and what YouTube would get.
        # Only a short wait: a slow GitHub upload must never make the chat
        # window time out. Anything later is fetched from /harness/published.
        task = getattr(runtimes, "publish_task", None)
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), 15)
            except Exception:
                pass
        published = list(getattr(runtimes, "publish_events", []))
        if published:
            runtimes.publish_events.clear()
        new_voice = app.state.voice_events[voice_mark:]
        first_voice = min((v["at"] for v in new_voice if v.get("ok")), default=None)
        return {
            "accepted": True,
            "seconds": round(time.perf_counter() - started, 1),
            # When the first spoken line was ready: what a viewer actually waits.
            "first_voice_s": (
                round(first_voice - started_wall, 1)
                if first_voice is not None
                else None
            ),
            "decision_ms": int(getattr(runtimes, "last_decision_seconds", 0) * 1000),
            "generation_ms": int(
                getattr(runtimes, "last_generation_seconds", 0) * 1000
            ),
            "voice": new_voice,
            "sound": app.state.sound,
            "published": published,
            "voices_real": app.state.tts is None,
            "mode": plan.decision.mode,
            "addressed": list(plan.decision.addressed or []),
            "spoke": [
                {"character": t.speaker, "kind": t.kind, "text": t.text}
                for t in plan.turns
                if t.text
            ],
            "skipped": [
                {"character": t.speaker, "why": t.skipped}
                for t in plan.turns
                if t.skipped
            ],
        }

    app.state.deliver = deliver

    @app.post("/harness/chat")
    async def chat(request: Request):
        body = await request.json()
        return JSONResponse(
            await deliver(
                str(body.get("user") or "@viewer"), str(body.get("text") or "")
            )
        )

    @app.get("/harness/state")
    async def state():
        teaching = session.teaching
        lesson = getattr(teaching, "coding_lesson", None)
        return JSONResponse(
            {
                "room": {
                    "active": session.active,
                    "cast": [c.id for c in session.room.characters],
                    "clients": len(session.client_uids()),
                },
                "teaching": teaching.session.snapshot(),
                "sound": app.state.sound,
                "voice": app.state.voice_events[-6:],
                "coding": lesson.prompt_context() if lesson else None,
            }
        )

    @app.get("/harness/clients")
    async def clients():
        return JSONResponse(session.status())

    @app.get("/harness/received")
    async def received():
        return JSONResponse(app.state.received[-200:])

    @app.post("/harness/push")
    async def push(request: Request):
        body = await request.json()
        await session.push(body.get("ops") or [])
        return JSONResponse({"ok": True})

    @app.post("/harness/publish")
    async def publish_route():
        """Publish the program on screen (the DEV chat window's /publish)."""
        runtimes = app.state.runtimes
        if runtimes is None:
            return JSONResponse({"ok": False, "reason": "no coding session yet"})
        outcome = await runtimes.publish_current(announce=True)
        runtimes.publish_events.clear()  # shown by /publish itself
        return JSONResponse(outcome)

    @app.get("/harness/published")
    async def published_route():
        """Publish results that finished after the chat reply (then forgotten)."""
        runtimes = app.state.runtimes
        events = list(getattr(runtimes, "publish_events", []))
        if events:
            runtimes.publish_events.clear()
        return JSONResponse({"published": events})

    @app.get("/harness/jobs")
    async def jobs_route():
        return JSONResponse(
            {
                "settings": app.state.publisher.settings.describe(),
                "jobs": [j.snapshot() for j in app.state.publisher.store.all()[-20:]],
            }
        )

    @app.post("/harness/card")
    async def card_route(request: Request):
        """Show (or hide) a viewer comment card on the Stage, for testing."""
        body = await request.json()
        await session.broadcast(
            {
                "type": "youtube-live-selected-message",
                "active": bool(body.get("active", True)),
                "id": str(body.get("id") or f"card-{time.time_ns()}"),
                "author": clean_viewer_text(str(body.get("author") or "@viewer"), 60),
                "message": clean_viewer_text(str(body.get("message") or ""), 180),
                "paid": False,
                "amount": "",
                "to": [str(x) for x in (body.get("to") or [])][:2],
            }
        )
        return JSONResponse({"ok": True})

    @app.post("/harness/sound")
    async def sound(request: Request):
        body = await request.json()
        state = str(body.get("state") or "")[:40]
        detail = str(body.get("detail") or "")[:300]
        if (
            state != "page-loaded"
            or app.state.sound.get("state") == "no stage page yet"
        ):
            app.state.sound = {"state": state, "detail": detail, "at": time.time()}
        if state in ("blocked", "failed"):
            logger.warning(
                f"DEV: the Stage could not play speech audio ({state}: {detail})"
            )
        return JSONResponse({"ok": True})

    @app.get("/harness/sound-guard.js")
    async def sound_guard():
        return Response(SOUND_GUARD_JS, media_type="application/javascript")

    @app.get("/" + STAGE_PAGE)
    async def stage_page():
        """The Stage, with the sound guard added (the file itself is untouched)."""
        page = root / "frontend" / STAGE_PAGE
        if not page.is_file():
            return Response("teaching-stage.html is missing", status_code=404)
        html = page.read_text(encoding="utf-8")
        tag = '<script src="/harness/sound-guard.js"></script>'
        lower = html.lower()
        at = lower.find("</head>")
        html = html[:at] + tag + html[at:] if at >= 0 else tag + html
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/healthz")
    async def healthz():
        return JSONResponse(
            {"ok": True, "cast": [c.id for c in session.room.characters]}
        )

    # ------------------------------------------------------------------
    # Static assets. Optional directories are mounted only when present, so a
    # trimmed DEV copy still boots.
    for route, folder, name in (
        ("/live2d-models", root / "live2d-models", "live2d"),
        ("/bg", root / "backgrounds", "bg"),
        ("/avatars", root / "avatars", "avatars"),
        ("/cache", root / "cache", "cache"),
    ):
        if folder.is_dir():
            app.mount(route, StaticFiles(directory=folder), name=name)
        else:
            logger.warning(f"DEV: {folder.name}/ is missing; {route} not served")
    app.mount("/", StaticFiles(directory=root / "frontend", html=True), name="frontend")
    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--no-browser-check",
        action="store_true",
        help="show web programs without testing them in a headless browser first",
    )
    parser.add_argument(
        "--fake-voices",
        action="store_true",
        help="speak with a test tone instead of the real voices",
    )
    args = parser.parse_args()
    logger.info(f"DEV server on http://{args.host}:{args.port}")
    logger.info(f"Stage: http://{args.host}:{args.port}/vr-agent/teaching-stage.html")
    app = create_app(
        tts=FakeTTS() if args.fake_voices else None,
        check_base_url=None
        if args.no_browser_check
        else f"http://{args.host}:{args.port}",
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
