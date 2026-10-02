"""How Code in Public programs must behave and look on a live stream.

Two things live here:

* The rules and design kit that go into the code-writing prompt, so the model
  knows it is LIVE: nobody can click, type, scroll or answer a dialog, and the
  result is watched on a 1920x1080 stream next to Mika and Luna.
* ``stage_preview_html``: a small autopilot added to the PREVIEW only (never to
  the source on screen). It closes dialogs, presses "start" once, and moves a
  virtual pointer so anything that reacts to the mouse still comes alive.

The design kit is grounded in the stream's own scene: a night-city window,
deep dusk blues, warm amber lamps and Mika's paint-splash colors. Programs
should look like they belong in that room, not like a generic web page.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Prompt text
# ---------------------------------------------------------------------------

LIVE_RULES = """THIS RUNS LIVE ON A YOUTUBE STREAM.
Viewers only WATCH. Nobody can click, tap, type, scroll, hover, press keys,
grant permissions or answer a dialog. The host cannot touch it either.
So the program must:
- start by itself the moment it loads, with no start button, no "click to
  begin", no "press any key";
- keep moving on its own forever (loop, restart or keep evolving), never
  freeze on a final frame;
- never use alert(), prompt(), confirm(), input fields, forms, file pickers,
  fullscreen, pointer lock, camera, microphone or audio;
- if it is a game, play itself: an AI or scripted player makes the moves and
  the score keeps changing;
- if it would normally react to the mouse, drive that motion yourself (a
  path, orbit or wander) so it still looks alive."""

SCREEN_LAYOUT = """Layout: fill the whole viewport (100vw x 100vh, margin 0, overflow hidden),
  no scrollbars. One big focal element, centered or on a clear third.
  A short title (2 to 5 words) saying what it is, placed so it never covers
  the focal element. Generous empty space; nothing touches the edges."""

SITE_LAYOUT = """Layout: a real website taller than the screen. Nobody can scroll, so the
  page scrolls ITSELF section by section (the template's tour) and loops.
  Each section fills most of one screen. Body text 16 to 22px via clamp(),
  headings 32 to 120px. Generous padding; nothing touches the edges."""

WEB_DESIGN_KIT = """DESIGN KIT (the stream's own look; use it unless the viewer asked for other colors):
Scene: a cozy apartment at night, a big window over a city, warm lamps inside.
Colors (CSS variables on :root):
  --night: #0f1d3a   page background, use a vertical gradient to #1b2f5c
  --dusk:  #2a4a86   panels, grid lines, secondary shapes
  --glass: #9cc2ff   cool highlights, outlines, window light
  --lamp:  #ffb55e   the warm accent, the one thing you want eyes on
  --paint: #ff6f9c   Mika's paint pink, sparing second accent
  --mint:  #5fe3c8   success, growth, positive numbers
  --paper: #f4ead8   text
Type: font-family: "Nunito", "Quicksand", "Avenir Next", "Segoe UI", system-ui,
  sans-serif. Rounded and friendly. It is read from across a room: body text
  at least 28px, numbers and titles 56px to 120px, weight 700 to 800 for
  anything important. Sentence case, no all-caps labels.
{layout}
Craft:
- <canvas> for anything with many moving things. Size it to the window times
  devicePixelRatio and redraw on resize.
- requestAnimationFrame with delta time, so speed does not depend on fps.
- Ease motion (easeInOutCubic, springs). Nothing teleports or jitters.
- Light, not flat: soft glow with ctx.shadowBlur or CSS drop-shadow in the
  accent color, gradients on shapes, a faint starfield or bokeh behind.
- Trails by painting a translucent background each frame.
- One memorable moment (a burst, a bloom, a reveal) that repeats every few
  seconds, so a viewer joining late still sees it.
- Rounded corners only where they mean something; no stack of identical
  cards, no generic dashboard look, no stock gradient washes.
- Keep it under about 200 lines. Clear names and a few comments, because the
  source is shown on stream."""

WEB_SKELETON = """Start from this shape (keep its structure, replace the drawing):
<!doctype html><html><head><meta charset="utf-8"><title>...</title><style>
:root{--night:#0f1d3a;--dusk:#2a4a86;--glass:#9cc2ff;--lamp:#ffb55e;--paint:#ff6f9c;--mint:#5fe3c8;--paper:#f4ead8}
html,body{margin:0;height:100%;overflow:hidden;background:linear-gradient(#0f1d3a,#1b2f5c);color:var(--paper);
font-family:"Nunito","Quicksand","Avenir Next","Segoe UI",system-ui,sans-serif}
canvas{position:fixed;inset:0;width:100vw;height:100vh}
h1{position:fixed;left:6vw;top:5vh;margin:0;font-size:clamp(40px,5vw,84px);font-weight:800;text-shadow:0 0 24px rgba(255,181,94,.45)}
</style></head><body><canvas id="c"></canvas><h1>...</h1><script>
const c=document.getElementById('c'),ctx=c.getContext('2d');
function fit(){const d=devicePixelRatio||1;c.width=innerWidth*d;c.height=innerHeight*d;ctx.setTransform(d,0,0,d,0,0)}
addEventListener('resize',fit);fit();
let last=performance.now();
function frame(t){const dt=Math.min(0.05,(t-last)/1000);last=t;
  ctx.fillStyle='rgba(15,29,58,0.25)';ctx.fillRect(0,0,innerWidth,innerHeight); // trails
  // update and draw here, using dt
  requestAnimationFrame(frame)}
requestAnimationFrame(frame);
</script></body></html>"""

PYTHON_DESIGN_KIT = """VISUALS (matplotlib), the stream's own look:
- plt.style.use('dark_background'); figure facecolor '#0f1d3a', axes
  facecolor '#13254a'; figsize=(16, 9), dpi=120.
- Colors in order: '#ffb55e', '#9cc2ff', '#ff6f9c', '#5fe3c8', '#f4ead8'.
- Big text: title 28pt bold, labels 18pt, ticks 14pt. Light grid alpha 0.15.
- Remove the top and right spines. Rounded line caps, linewidth 3 or more.
- Save once with plt.savefig('output.png', bbox_inches='tight'); never
  plt.show(). Print a short readable summary to stdout too."""

PYTHON_LIVE_RULES = """THIS RUNS LIVE ON A YOUTUBE STREAM.
Nobody can type or click. Never call input(), never wait for keys, never open
windows. The program runs top to bottom on its own and finishes within a few
seconds. Print results clearly, because the terminal is shown on stream."""


def live_rules(language: str, kind: str = "") -> str:
    """Everything the code-writing prompt needs to know about being live.

    The plain-canvas skeleton is only for web programs without a template
    (it would fight a template), and Python gets no chart styling here: each
    Python kind's own guide says whether and how to draw (a lesson draws
    nothing).
    """
    if language == "web":
        layout = SITE_LAYOUT if kind == "web_site" else SCREEN_LAYOUT
        rules = f"{LIVE_RULES}\n\n{WEB_DESIGN_KIT.replace('{layout}', layout)}"
        if kind in ("", "web_canvas"):
            rules += f"\n\n{WEB_SKELETON}"
        return rules
    if not kind:
        return f"{PYTHON_LIVE_RULES}\n\n{PYTHON_DESIGN_KIT}"
    return PYTHON_LIVE_RULES


# A short line for the characters' spoken turns, so they never ask anyone to
# click, type or press anything.
SPEECH_LIVE_NOTE = (
    "This is a live YouTube stream: viewers can only watch and type in chat. "
    "Never ask anyone to click, tap, press a key, scroll or type into the program; "
    "the program runs by itself on screen."
)


# ---------------------------------------------------------------------------
# Preview autopilot
# ---------------------------------------------------------------------------

# Runs inside the preview frame only. It never changes the program's logic;
# it removes the things a live stream cannot do and plays the part of a
# viewer's mouse for programs that wait for one.
AUTOPILOT_JS = r"""
(function () {
  if (window.__vrStageAutopilot) return;
  window.__vrStageAutopilot = true;
  // Report where an error happened, so the one repair pass can go to the line.
  window.addEventListener("error", function (e) {
    try { console.warn("__vr_error__" + JSON.stringify({ m: String(e.message || ""), l: e.lineno || 0 })); } catch (_) {}
  });
  // Dialogs would freeze the stream with nobody to close them.
  window.alert = function () {};
  window.confirm = function () { return true; };
  window.prompt = function (_m, d) { return d == null ? "" : String(d); };

  function fire(target, type, x, y, extra) {
    if (!target) return;
    var init = Object.assign({ bubbles: true, cancelable: true, clientX: x, clientY: y,
      pointerId: 1, pointerType: "mouse", isPrimary: true, view: window }, extra || {});
    var Ctor = type.indexOf("pointer") === 0 && window.PointerEvent ? PointerEvent : MouseEvent;
    try { target.dispatchEvent(new Ctor(type, init)); } catch (_) {}
  }
  function key(code, k) {
    var init = { bubbles: true, cancelable: true, key: k, code: code };
    [document, window].forEach(function (t) {
      try { t.dispatchEvent(new KeyboardEvent("keydown", init)); } catch (_) {}
      try { t.dispatchEvent(new KeyboardEvent("keyup", init)); } catch (_) {}
    });
  }
  function visible(el) {
    var r = el.getBoundingClientRect();
    var s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  }

  // Press "start" once: the first visible button, plus Space and Enter, the
  // way a viewer would begin a page that waits for them.
  setTimeout(function () {
    var buttons = Array.prototype.filter.call(
      document.querySelectorAll("button, [role=button], input[type=button], input[type=submit]"),
      visible
    );
    if (buttons.length) {
      var b = buttons[0].getBoundingClientRect();
      var bx = b.left + b.width / 2, by = b.top + b.height / 2;
      fire(buttons[0], "pointerdown", bx, by); fire(buttons[0], "mousedown", bx, by);
      fire(buttons[0], "pointerup", bx, by); fire(buttons[0], "mouseup", bx, by);
      try { buttons[0].click(); } catch (_) {}
    }
    key("Space", " ");
    key("Enter", "Enter");
  }, 1200);

  // A virtual pointer that wanders smoothly, so hover and follow effects move.
  var start = performance.now();
  var lastClick = 0;
  function wander(t) {
    var s = (t - start) / 1000;
    var w = innerWidth, h = innerHeight;
    var x = w * (0.5 + 0.32 * Math.sin(s * 0.53) * Math.cos(s * 0.21));
    var y = h * (0.52 + 0.28 * Math.sin(s * 0.37 + 1.3));
    var target = document.elementFromPoint(x, y) || document.body;
    fire(target, "pointermove", x, y);
    fire(target, "mousemove", x, y);
    // Every few seconds, tap the drawing surface (never links or buttons),
    // for programs that spawn things where you click.
    if (t - lastClick > 3200 && target && (target.tagName === "CANVAS" || target === document.body)) {
      lastClick = t;
      fire(target, "pointerdown", x, y); fire(target, "mousedown", x, y);
      fire(target, "pointerup", x, y); fire(target, "mouseup", x, y);
      fire(target, "click", x, y);
    }
    requestAnimationFrame(wander);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { requestAnimationFrame(wander); });
  } else {
    requestAnimationFrame(wander);
  }
  // No scrollbars on stream.
  try { document.documentElement.style.overflow = "hidden"; } catch (_) {}
})();
"""


# Markup positions only; this never looks at viewer text.
_HEAD = re.compile(r"<head(?:\s[^>]*)?>", re.I)
_HTML = re.compile(r"<html(?:\s[^>]*)?>", re.I)


def preview_line_to_source(code: str, line: int) -> int:
    """A line number in the preview document, as a line of the source on screen.

    The autopilot is inserted right after <head> (or <html>, or at the start),
    which pushes every later line down. 0 means the line is not in the source.
    """
    html = str(code or "")
    shift = AUTOPILOT_JS.count("\n")
    at = 0
    head = _HEAD.search(html)
    root = _HTML.search(html)
    if head:
        at = head.end()
    elif root:
        at = root.end()
    insert_line = html.count("\n", 0, at) + 1
    if line <= 0:
        return 0
    if line <= insert_line:
        return line
    if line < insert_line + shift:
        return 0  # inside the autopilot
    return line - shift


def stage_preview_html(code: str) -> str:
    """The program as the preview runs it: the source plus the autopilot.

    The autopilot goes first in <head>, so dialogs are already disabled
    before the program's own script runs. The source shown on screen is
    never changed.
    """
    html = str(code or "")
    if not html.strip():
        return html
    script = f"<script>{AUTOPILOT_JS}</script>"
    head = _HEAD.search(html)
    if head:
        return html[: head.end()] + script + html[head.end() :]
    root = _HTML.search(html)
    if root:
        return html[: root.end()] + "<head>" + script + "</head>" + html[root.end() :]
    return script + html
