"""Browser tests for the Playwright chat reader against a real YouTube chat DOM.

The fixture is a live chat DOM captured from YouTube with viewer names and
message text anonymised. Structure, ids, placeholders, emoji markup and the
Top chat / Live chat selector are untouched.
"""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

pw = pytest.importorskip("playwright.async_api")

from open_llm_vtuber.live.youtube_playwright_chat import (  # noqa: E402
    OBSERVER_JS,
    YouTubePlaywrightChatSource,
)

FIXTURE = Path(__file__).parent / "fixtures" / "youtube_live_chat_dom.html"


def run(coro):
    return asyncio.run(coro)


async def open_fixture(p):
    try:
        browser = await p.chromium.launch()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Chromium unavailable: {exc}")
    page = await browser.new_page()
    await page.route("**/*", lambda route: route.abort())
    emitted = []
    await page.expose_binding(
        "__vrAgentEmit", lambda _src, batch: emitted.extend(batch)
    )
    await page.set_content(
        "<html><body>" + FIXTURE.read_text(encoding="utf-8") + "</body></html>",
        wait_until="domcontentloaded",
    )
    return browser, page, emitted


ADD_MESSAGE_JS = """
([id, html, delayText]) => {
  const items = document.querySelector('yt-live-chat-item-list-renderer #items');
  const template = items.querySelector('yt-live-chat-text-message-renderer');
  const el = template.cloneNode(true);
  el.id = id;
  const msg = el.querySelector('#message');
  el.querySelector('#author-name').firstChild.data = '@new-viewer';
  if (delayText) {
    msg.innerHTML = '';
    items.appendChild(el);
    setTimeout(() => { msg.innerHTML = html; }, 120);
  } else {
    msg.innerHTML = html;
    items.appendChild(el);
  }
}
"""


def test_observer_extracts_only_new_real_messages():
    async def scenario():
        async with pw.async_playwright() as p:
            browser, page, emitted = await open_fixture(p)
            beat = await page.evaluate(OBSERVER_JS)
            assert beat["attached"] is True
            assert beat["selector"] == "yt-live-chat-item-list-renderer #items"
            assert beat["seen"] == 65  # captured backlog is remembered, not emitted

            await page.wait_for_timeout(200)
            assert emitted == []

            await page.evaluate(
                ADD_MESSAGE_JS,
                [
                    "NEW1",
                    'Can you clap? <img class="small-emoji emoji" alt="😂" '
                    'shared-tooltip-text=":face_with_tears_of_joy:"> '
                    '<img class="emoji" alt="face-blue-smiling" data-emoji-id="x/y" '
                    'shared-tooltip-text=":face-blue-smiling:"> <b>hi</b>',
                    False,
                ],
            )
            # Non-message rows YouTube also inserts into #items.
            await page.evaluate(
                """() => {
                  const items = document.querySelector('yt-live-chat-item-list-renderer #items');
                  items.appendChild(document.createElement('yt-live-chat-placeholder-item-renderer'));
                  const sys = document.createElement('yt-live-chat-viewer-engagement-message-renderer');
                  sys.id = 'SYS1'; sys.textContent = 'Welcome to live chat!';
                  items.appendChild(sys);
                }"""
            )
            # Duplicate id must not be emitted twice.
            await page.evaluate(ADD_MESSAGE_JS, ["NEW1", "duplicate", False])
            # Polymer-style late text stamping.
            await page.evaluate(ADD_MESSAGE_JS, ["NEW2", "late text arrives", True])
            await page.wait_for_timeout(700)

            ids = [m["id"] for m in emitted]
            assert ids == ["NEW1", "NEW2"]
            first = emitted[0]
            assert first["author"] == "@new-viewer"
            assert first["kind"] == "text"
            assert "😂" in first["text"]
            assert ":face-blue-smiling:" in first["text"]
            assert "<" not in first["text"] and "hi" in first["text"]
            assert emitted[1]["text"] == "late text arrives"
            await browser.close()

    run(scenario())


def test_deleted_messages_are_skipped_and_list_replacement_reattaches():
    async def scenario():
        async with pw.async_playwright() as p:
            browser, page, emitted = await open_fixture(p)
            await page.evaluate(OBSERVER_JS)
            await page.evaluate(
                """() => {
                  const items = document.querySelector('yt-live-chat-item-list-renderer #items');
                  const el = items.querySelector('yt-live-chat-text-message-renderer').cloneNode(true);
                  el.id = 'DEL1'; el.setAttribute('is-deleted', '');
                  items.appendChild(el);
                }"""
            )
            # YouTube swaps the whole #items node (e.g. Top chat -> Live chat).
            await page.evaluate(
                """() => {
                  const old = document.querySelector('yt-live-chat-item-list-renderer #items');
                  const fresh = old.cloneNode(false);
                  old.replaceWith(fresh);
                }"""
            )
            beat = await page.evaluate(OBSERVER_JS)  # second call = heartbeat
            assert beat["reattached"] is True and beat["attached"] is True
            await page.evaluate(
                ADD_MESSAGE_JS.replace(
                    "const template = items.querySelector('yt-live-chat-text-message-renderer');",
                    "const template = window.__tpl || (window.__tpl = document.createElement('div'));",
                ).replace(
                    "const el = template.cloneNode(true);",
                    """
                const el = document.createElement('yt-live-chat-text-message-renderer');
                el.innerHTML = '<span id="author-name">x</span><span id="message"></span>';
            """,
                ),
                ["AFTER1", "hello again", False],
            )
            await page.wait_for_timeout(300)
            assert [m["id"] for m in emitted] == ["AFTER1"]
            assert beat["ended_hint"] == ""
            await browser.close()

    run(scenario())


def make_source(**overrides):
    cfg = dict(
        video_id=None,
        channel_id="",
        channel_handle="",
        ignore_owner_messages=False,
        prefer_live_chat_mode=True,
    )
    cfg.update(overrides)
    return YouTubePlaywrightChatSource(SimpleNamespace(**cfg))


def test_live_chat_mode_is_selected_once_and_failure_is_graceful():
    async def scenario():
        async with pw.async_playwright() as p:
            browser, page, _ = await open_fixture(p)
            # Make the captured dropdown behave: trigger opens it, an item sets the label.
            await page.evaluate(
                """() => {
                  const label = document.querySelector('#live-chat-view-selector-sub-menu #label-text');
                  const trigger = document.querySelector('#live-chat-view-selector-sub-menu #label');
                  const dropdown = document.querySelector('#live-chat-view-selector-sub-menu tp-yt-iron-dropdown');
                  dropdown.style.cssText = 'display:none;position:fixed;top:0;left:0;z-index:9;background:#fff';
                  trigger.addEventListener('click', () => { dropdown.style.display = 'block'; window.__opens = (window.__opens||0)+1; });
                  dropdown.querySelectorAll('tp-yt-paper-listbox a').forEach(a => {
                    a.style.display = 'block'; a.style.height = '30px';
                    a.addEventListener('click', () => {
                      label.textContent = a.querySelector('.item').firstChild.data.trim();
                      dropdown.style.display = 'none';
                    });
                  });
                }"""
            )
            source = make_source()
            await source._select_live_chat_mode(page)
            assert source.health.chat_mode == "live"
            # Already in Live chat: no further clicks.
            await source._select_live_chat_mode(page)
            assert await page.evaluate("() => window.__opens") == 1

            # A page without the selector must not raise.
            await page.set_content("<html><body><div id='items'></div></body></html>")
            await source._select_live_chat_mode(page)
            assert source.health.chat_mode.startswith("top")
            await browser.close()

    run(scenario())


def test_binding_payload_is_validated_before_reaching_the_buffer():
    source = make_source(ignore_owner_messages=True)
    now = datetime.now(timezone.utc)
    ok = source._to_message(
        {
            "id": "A1",
            "author": "@viewer",
            "text": "hi",
            "kind": "paid",
            "amount": "₱100",
        },
        now,
    )
    assert (
        ok.author_channel_id == "name:@viewer"
        and ok.kind == "paid"
        and ok.amount == "₱100"
    )
    assert source._to_message({"id": "", "text": "x"}, now) is None
    assert source._to_message({"id": "B", "text": ""}, now) is None
    assert source._to_message("not a dict", now) is None
    assert (
        source._to_message({"id": "C", "text": "mine", "author_type": "owner"}, now)
        is None
    )
    long = source._to_message({"id": "D", "text": "x" * 5000, "author": "y" * 500}, now)
    assert len(long.text) == 500 and len(long.author_display_name) == 100

    received = []

    async def sink(messages):
        received.extend(messages)

    source._on_messages = sink
    run(
        source._on_binding(
            None, [{"id": "E", "author": "@a", "text": "yo"}, {"bad": 1}]
        )
    )
    assert [m.message_id for m in received] == ["E"]
    assert source.health.messages_received == 1


def test_source_readiness_rules():
    assert make_source(video_id="dQw4w9WgXcQ").ready()[0]
    assert not make_source(video_id="bad").ready()[0]
    assert make_source(channel_id="UCDhDUZCr4Hqsf9jUlKIQ4PQ").ready()[0]
    assert make_source(channel_handle="@selwynbuilds").ready()[0]
    assert not make_source(channel_id="${YOUTUBE_CHANNEL_ID}").ready()[0]
    assert json.dumps(make_source().status())


def test_supervisor_attaches_delivers_and_recovers_from_a_closed_page(monkeypatch):
    """Full reader loop against a local copy of the chat page."""
    import http.server
    import threading

    import open_llm_vtuber.live.youtube_playwright_chat as mod

    body = (
        "<html><body>" + FIXTURE.read_text(encoding="utf-8") + "</body></html>"
    ).encode()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(
        mod,
        "LIVE_CHAT_URL",
        f"http://127.0.0.1:{server.server_address[1]}/live_chat?v={{video_id}}",
    )
    config = SimpleNamespace(
        video_id="dQw4w9WgXcQ",
        channel_id="",
        channel_handle="",
        ignore_owner_messages=False,
        prefer_live_chat_mode=True,
        discovery_retry_seconds=1,
        live_check_interval_seconds=0,
        answer_backlog_on_start=0,
        playwright_headless=True,
        playwright_user_data_dir="",
        playwright_user_agent="",
        playwright_heartbeat_seconds=0.3,
        playwright_chat_load_timeout_seconds=10,
        playwright_restart_min_seconds=0.2,
        playwright_restart_max_seconds=1,
        playwright_page_recycle_hours=0,
    )
    source = YouTubePlaywrightChatSource(config)
    received = []

    async def on_messages(messages):
        received.extend(messages)

    add_js = ADD_MESSAGE_JS

    async def wait_for(cond, timeout=15):
        end = asyncio.get_running_loop().time() + timeout
        while not cond():
            if asyncio.get_running_loop().time() > end:
                raise AssertionError("timed out")
            await asyncio.sleep(0.1)

    async def scenario():
        task = asyncio.create_task(source.run(on_messages))
        try:
            await wait_for(lambda: source.health.chat_attached)
            await source._page.evaluate(add_js, ["LIVE1", "first message", False])
            await wait_for(lambda: len(received) == 1)

            # Simulate the chat tab dying; the supervisor must restart it.
            await source._page.close()
            await wait_for(
                lambda: source.health.restarts >= 1 and source.health.chat_attached
            )
            await source._page.evaluate(add_js, ["LIVE2", "after recovery", False])
            await wait_for(lambda: len(received) == 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    try:
        run(scenario())
    except Exception as exc:
        if "Executable doesn't exist" in str(exc):
            pytest.skip("Chromium unavailable")
        raise
    finally:
        server.shutdown()

    assert [m.text for m in received] == ["first message", "after recovery"]
    assert received[0].author_display_name == "@new-viewer"
    assert source.health.browser_running is False  # torn down on cancel


def test_browser_does_not_announce_headless_chrome():
    """YouTube live chat shows 'update your browser' to HeadlessChrome."""
    source = make_source()
    source.config.playwright_headless = True
    source.config.playwright_user_data_dir = ""
    source.config.playwright_user_agent = ""

    async def scenario():
        try:
            await source._ensure_browser()
        except Exception as exc:
            if "Executable doesn't exist" in str(exc):
                pytest.skip("Chromium unavailable")
            raise
        try:
            page = await source._context.new_page()
            await page.set_content("<p>x</p>")
            return await page.evaluate("navigator.userAgent")
        finally:
            await source._teardown(full=True)

    ua = run(scenario())
    assert "Headless" not in ua and "Chrome/" in ua


def test_first_attach_answers_the_newest_backlog_only():
    async def scenario():
        async with pw.async_playwright() as p:
            browser, page, emitted = await open_fixture(p)
            await page.evaluate("n => { window.__vrAgentBacklog = n; }", 2)
            await page.evaluate(OBSERVER_JS)
            await page.wait_for_timeout(300)
            ids = await page.evaluate(
                "() => [...document.querySelectorAll('yt-live-chat-item-list-renderer #items "
                "yt-live-chat-text-message-renderer')].slice(-2).map(e => e.id)"
            )
            assert [m["id"] for m in emitted] == ids
            # A re-attach (list replaced) must not replay anything.
            await page.evaluate(
                "() => { const o = document.querySelector('yt-live-chat-item-list-renderer #items');"
                " o.replaceWith(o.cloneNode(true)); }"
            )
            await page.evaluate(OBSERVER_JS)
            await page.wait_for_timeout(300)
            assert len(emitted) == 2
            await browser.close()

    run(scenario())
