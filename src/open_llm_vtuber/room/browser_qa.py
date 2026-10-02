"""Does the web program actually work and look alive? Checked in a real browser.

"It ran" and "it looks right" are different. Before a web program is called
finished, it is opened in headless Chromium exactly the way the Stage shows
it (same sandbox, same security policy, same autopilot) and these are
checked, deterministically, in about three seconds:

* uncaught errors and console errors
* files that failed to load, and anything the security policy blocked
* a blank or near-blank picture
* no motion between two moments (live programs must keep moving)

The result is plain text the code writer can act on, so one repair pass can
fix real problems. No vision model is involved: GPT-4o-mini can read images,
but a screenshot critique would add a model call and seconds to every build;
the screenshot is kept for logs and a later opt-in critique.

One browser is started on first use and reused, so a check costs a new page,
not a new browser.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from loguru import logger

from .stage_design import preview_line_to_source, stage_preview_html

QA_PAGE = "/vr-agent/stage-qa.html"
IGNORED_CONSOLE = (
    "favicon",
    "AudioContext was not allowed",
    "Unrecognized feature",
    "GPU stall",
    "GroupMarkerNotSet",
    "Automatic fallback to software WebGL",
    # p5.js listens for device motion; the sandbox reports that. Harmless.
    "Permissions policy violation: accelerometer",
    "Permissions policy violation: gyroscope",
    "Permissions policy violation: magnetometer",
)

# Measured inside the browser: the screenshot is drawn on a canvas in the QA
# page (same origin) and summarised, so the server needs no image library.
_PIXEL_STATS_JS = """
async ([a, b]) => {
  async function pixels(b64) {
    const img = new Image();
    img.src = "data:image/png;base64," + b64;
    await img.decode();
    const w = 320, h = 180;
    const c = document.createElement("canvas");
    c.width = w; c.height = h;
    const ctx = c.getContext("2d");
    ctx.drawImage(img, 0, 0, w, h);
    return ctx.getImageData(0, 0, w, h).data;
  }
  const p = await pixels(a), q = await pixels(b);
  const n = p.length / 4;
  let sum = 0, sum2 = 0, diff = 0, changed = 0;
  const colours = new Map();
  for (let i = 0; i < p.length; i += 4) {
    const g = 0.299 * p[i] + 0.587 * p[i + 1] + 0.114 * p[i + 2];
    sum += g; sum2 += g * g;
    const key = (p[i] >> 4) * 256 + (p[i + 1] >> 4) * 16 + (p[i + 2] >> 4);
    colours.set(key, (colours.get(key) || 0) + 1);
    const d = Math.abs(p[i] - q[i]) + Math.abs(p[i + 1] - q[i + 1]) + Math.abs(p[i + 2] - q[i + 2]);
    diff += d;
    if (d > 24) changed += 1;
  }
  const mean = sum / n;
  let top = 0;
  for (const v of colours.values()) top = Math.max(top, v);
  return {
    brightness: mean,
    contrast: Math.sqrt(Math.max(0, sum2 / n - mean * mean)),
    dominant_share: top / n,
    colours: colours.size,
    motion: diff / (n * 3),
    changed_share: changed / n,
  };
}
"""


@dataclass
class BrowserReport:
    ok: bool
    errors: list[str] = field(default_factory=list)
    failed_files: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    blank: bool = False
    moving: bool = True
    stats: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0
    screenshot_png: Optional[bytes] = field(default=None, repr=False)
    skipped: str = ""
    # One-screen programs only: the page is taller or wider than the screen,
    # so part of it is cut off on stream. "" when everything fits.
    overflow: str = ""
    # Buttons and links that look clickable but do nothing.
    dead_controls: list[str] = field(default_factory=list)

    def problems(self) -> list[str]:
        out = []
        out += [f"JavaScript error: {e}" for e in self.errors[:5]]
        out += [f"File failed to load: {f}" for f in self.failed_files[:5]]
        out += [
            f"Blocked by the stage security policy (only /stage-libs/ and "
            f"/stage-assets/ files are allowed): {b}"
            for b in self.blocked[:3]
        ]
        if self.blank:
            out.append(
                "The screen is blank or almost one flat colour: nothing visible is drawn."
            )
        if self.overflow:
            out.append(
                f"Content is cut off: {self.overflow}. Everything must fit in one "
                "screen: use fewer items, two columns or smaller type."
            )
        if self.dead_controls:
            out.append(
                "Buttons or links do nothing when clicked: "
                + ", ".join(f"'{d}'" for d in self.dead_controls[:6])
                + ". Every button and link must work: scroll to a section that "
                "exists (href=\"#id\" and an element with that id) or do something "
                "visible. A form shows a 'Thanks, this is a demo' message."
            )
        if not self.moving and not self.blank:
            out.append(
                "Nothing moves: the picture is identical a moment later. Live programs "
                "must animate on their own (requestAnimationFrame, tweens or a timer)."
            )
        return out

    def feedback(self) -> str:
        return "\n".join(f"- {p}" for p in self.problems())

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "problems": self.problems(),
            "stats": {
                k: round(v, 3) if isinstance(v, float) else v
                for k, v in self.stats.items()
            },
            "seconds": round(self.seconds, 2),
            "skipped": self.skipped,
        }


def _launch_args() -> list[str]:
    # Software WebGL so Three.js and Phaser render in headless Chromium.
    return ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]


class BrowserQA:
    """Owns one headless browser; `check` is safe to call repeatedly."""

    def __init__(
        self, base_url: str, settle_seconds: float = 2.4, timeout_seconds: float = 15.0
    ):
        self.base_url = base_url.rstrip("/")
        self.settle_seconds = settle_seconds
        self.timeout_seconds = timeout_seconds
        self._playwright = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def _ensure_browser(self):
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        from playwright.async_api import async_playwright

        if self._playwright is None:
            self._playwright = await async_playwright().start()
        executable = os.environ.get("VR_QA_CHROMIUM") or None
        self._browser = await self._playwright.chromium.launch(
            executable_path=executable, args=_launch_args()
        )
        return self._browser

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            self._browser = None
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None

    async def check(self, source: str, allow_scroll: bool = False) -> BrowserReport:
        """allow_scroll: a website that scrolls itself may be taller than the screen."""
        started = time.perf_counter()
        async with self._lock:
            # Starting the browser is the checker's own business: if that is
            # slow or impossible, the check is skipped, never blamed on the
            # program (which would trigger a pointless repair).
            try:
                await asyncio.wait_for(self._ensure_browser(), 60)
            except Exception as exc:
                logger.warning(f"Browser check unavailable: {exc}")
                return BrowserReport(
                    ok=True,
                    skipped=f"browser unavailable: {str(exc)[:160]}",
                    seconds=time.perf_counter() - started,
                )
            try:
                report = await asyncio.wait_for(
                    self._check(source, allow_scroll), self.timeout_seconds
                )
            except asyncio.TimeoutError:
                # The page itself froze: that is a real problem with the program.
                report = BrowserReport(
                    ok=False,
                    errors=[
                        "The page froze: it did not respond within the time limit."
                    ],
                )
            except Exception as exc:
                logger.warning(f"Browser check failed to run: {exc}")
                report = BrowserReport(ok=True, skipped=str(exc)[:200])
        report.seconds = time.perf_counter() - started
        return report

    async def _check(self, source: str, allow_scroll: bool = False) -> BrowserReport:
        browser = await self._ensure_browser()
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        errors: list[str] = []
        failed: list[str] = []
        blocked: list[str] = []
        lines: dict[str, int] = {}  # error message -> line in the source

        def on_console(message) -> None:
            text = message.text or ""
            if text.startswith("__vr_error__"):
                try:
                    data = json.loads(text[len("__vr_error__") :])
                    line = preview_line_to_source(source, int(data.get("l") or 0))
                    if line:
                        lines[_bare(str(data.get("m") or ""))] = line
                except (ValueError, TypeError):
                    pass
                return
            if any(skip in text for skip in IGNORED_CONSOLE):
                return
            if "Content Security Policy" in text or "Content-Security-Policy" in text:
                blocked.append(text[:300])
            elif message.type == "error":
                errors.append(text[:300])

        def on_failed(request) -> None:
            if "favicon" not in request.url:
                failed.append(f"{request.url[:200]} ({request.failure or 'failed'})")

        def on_response(response) -> None:
            if response.status >= 400 and "favicon" not in response.url:
                failed.append(f"{response.url[:200]} (HTTP {response.status})")

        page = await context.new_page()
        page.on("console", on_console)
        page.on("pageerror", lambda exc: errors.append(str(exc)[:300]))
        page.on("requestfailed", on_failed)
        page.on("response", on_response)
        try:
            await page.goto(self.base_url + QA_PAGE, wait_until="load")
            await page.evaluate(
                "(html) => window.loadProgram(html)", stage_preview_html(source)
            )
            frame_el = await page.wait_for_selector("#preview")
            await asyncio.sleep(self.settle_seconds)
            first = await frame_el.screenshot(type="png")
            await asyncio.sleep(0.7)
            second = await frame_el.screenshot(type="png")
            stats = await page.evaluate(
                _PIXEL_STATS_JS,
                [base64.b64encode(first).decode(), base64.b64encode(second).decode()],
            )
            overflow = ""
            dead: list[str] = []
            frame = await frame_el.content_frame()
            if frame is not None:
                if not allow_scroll:
                    overflow = _describe_overflow(await frame.evaluate(_SIZE_JS))
                try:
                    dead = [str(d)[:60] for d in (await frame.evaluate(_DEAD_JS) or [])]
                except Exception:  # a page that navigated away: nothing to judge
                    dead = []
        finally:
            await context.close()

        # Failed loads are reported once each, without the console duplicate.
        errors = [e for e in errors if "Failed to load resource" not in e]
        errors = [_with_line(e, lines, source) for e in errors]
        blank = stats["contrast"] < 4.0 or stats["dominant_share"] > 0.97
        moving = stats["motion"] > 0.15 or stats["changed_share"] > 0.002
        ok = (
            not errors
            and not failed
            and not blocked
            and not blank
            and moving
            and not overflow
        )
        # Dead buttons do not fail the check: a repair pass costs a whole new
        # build on stream, and nobody can click on the Stage anyway. The
        # published page gets a safety net for them instead (bundle.py).
        return BrowserReport(
            ok=ok,
            errors=_unique(errors),
            failed_files=_unique(failed),
            blocked=_unique(blocked),
            blank=blank,
            moving=moving,
            stats=stats,
            screenshot_png=second,
            overflow=overflow,
            dead_controls=_unique(dead),
        )


# Buttons and links that look clickable but have nothing behind them. A
# handler anywhere up the tree (or on document or window) counts, so
# delegated click handling is never reported.
_DEAD_JS = r"""
() => {
  const delegated = !!(window.__vrClick || document.__vrClick || window.onclick || document.onclick);
  const handled = (el) => {
    for (let n = el; n && n !== document; n = n.parentNode) {
      if (n.__vrClick || n.onclick || (n.getAttribute && n.getAttribute("onclick"))) return true;
    }
    return delegated;
  };
  const name = (el) => (el.textContent || el.value || el.getAttribute("aria-label") || "")
    .trim().replace(/\s+/g, " ").slice(0, 40);
  const dead = [];
  for (const a of document.querySelectorAll("a")) {
    const label = name(a);
    if (!label) continue;
    const href = (a.getAttribute("href") || "").trim();
    if (href.startsWith("#") && href.length > 1) {
      let id = href.slice(1);
      try { id = decodeURIComponent(id); } catch (_) {}
      if (!document.getElementById(id) && !handled(a)) dead.push(label + " (nothing has id " + id + ")");
      continue;
    }
    if (href && href !== "#" && !/^javascript:/i.test(href)) continue;
    if (!handled(a)) dead.push(label);
  }
  for (const b of document.querySelectorAll("button, [role=button], input[type=button], input[type=submit]")) {
    if (b.disabled) continue;
    if (b.form && (b.type === "submit" || b.tagName === "BUTTON")) {
      if (handled(b) || b.form.__vrClick || b.form.onsubmit) continue;
      dead.push((name(b) || "a form button") + " (the form does nothing)");
      continue;
    }
    if (!handled(b)) dead.push(name(b) || "a button");
  }
  return Array.from(new Set(dead)).slice(0, 8);
}
"""


# How big the page really is, measured inside the program's own frame.
_SIZE_JS = """
() => {
  const d = document.documentElement, b = document.body || d;
  return {
    height: Math.max(d.scrollHeight, b.scrollHeight),
    width: Math.max(d.scrollWidth, b.scrollWidth),
    screenHeight: innerHeight,
    screenWidth: innerWidth,
  };
}
"""


def _describe_overflow(size: dict) -> str:
    try:
        h, w = int(size["height"]), int(size["width"])
        sh, sw = int(size["screenHeight"]), int(size["screenWidth"])
    except (KeyError, TypeError, ValueError):
        return ""
    if sh <= 0 or sw <= 0:
        return ""
    # A few pixels of slack (inline canvas gaps, rounding).
    if h > sh * 1.04 + 4:
        return f"the page is {h}px tall but the screen is {sh}px"
    if w > sw * 1.04 + 4:
        return f"the page is {w}px wide but the screen is {sw}px"
    return ""


def _bare(message: str) -> str:
    """'Uncaught SyntaxError: X' and 'X' are the same error."""
    text = message.strip()
    for prefix in ("Uncaught ",):
        if text.startswith(prefix):
            text = text[len(prefix) :]
    head, sep, rest = text.partition(": ")
    if sep and head.replace("Error", "").isalpha() and head.endswith("Error"):
        return rest.strip()
    return text


def _with_line(error: str, lines: dict[str, int], source: str) -> str:
    line = lines.get(_bare(error))
    if not line:
        return error
    code_lines = str(source or "").splitlines()
    text = code_lines[line - 1].strip()[:120] if 0 < line <= len(code_lines) else ""
    where = f" (line {line}" + (f": {text}" if text else "") + ")"
    return error + where


def _unique(items: list[str]) -> list[str]:
    seen, out = set(), []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
