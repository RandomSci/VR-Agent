"""games.json and the gallery page that lists every published creation.

games.json is the registry (no database): one entry per published game,
merged by job id so publishing the same job twice never duplicates it. The
gallery page is regenerated from it on every publish.
"""

from __future__ import annotations

import html
import json
import re
import time
from typing import Any

from .provenance import CreationJob, is_safe_slug


_TITLE_LEAD = re.compile(
    r"^(?:(?:building|making|creating|writing|coding|build|make|create|write|code)"
    r"\s+)?(?:(?:a|an|the|my|your|our)\s+)?(?:(?:new|simple|cool|fun)\s+)*",
    re.I,
)


def clean_title(text: str) -> str:
    """'building a new space shooter game' -> 'Space shooter game' for the gallery."""
    raw = " ".join(str(text or "").split()).strip(" .")
    trimmed = _TITLE_LEAD.sub("", raw).strip()
    title = trimmed or raw
    return title[:1].upper() + title[1:]


def entry_for(job: CreationJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "slug": job.project_slug,
        "title": job.title or job.project_slug,
        "requested_by": job.viewer_display_name or "a viewer",
        "built_by": job.built_by,
        "kind": job.project_type,
        "url": job.public_url,
        "published_at": int(job.published_at or time.time()),
        "has_thumbnail": False,
    }


def merge_registry(existing: bytes | None, entry: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(existing.decode("utf-8")) if existing else {}
    except (ValueError, UnicodeDecodeError):
        data = {}
    games = [
        g
        for g in data.get("games", [])
        if isinstance(g, dict) and is_safe_slug(str(g.get("slug", "")))
    ]
    games = [g for g in games if g.get("job_id") != entry["job_id"]]
    games.append(entry)
    games.sort(key=lambda g: g.get("published_at", 0), reverse=True)
    return {"title": "Games built live by Mika and Luna", "games": games}


def slug_owner(existing: bytes | None, slug: str) -> str:
    """The job id that already owns this slug in the published registry, if any."""
    try:
        data = json.loads(existing.decode("utf-8")) if existing else {}
    except (ValueError, UnicodeDecodeError):
        return ""
    for game in data.get("games", []):
        if isinstance(game, dict) and game.get("slug") == slug:
            return str(game.get("job_id") or "")
    return ""


def _safe_url(url: str) -> str:
    url = str(url or "").strip()
    return (
        url if url.startswith("https://") and '"' not in url and "<" not in url else ""
    )


ADMIN_URL = "http://127.0.0.1:12393/vr-agent/admin.html"


def render_gallery(
    registry: dict[str, Any], live_url: str = "", admin_url: str = ADMIN_URL
) -> str:
    """The gallery: search by title or name, share any game, watch the stream.

    The Admin link opens the admin page on the streamer's own computer; for
    everyone else it simply does not load, and no password lives here.
    """
    cards = []
    builders: set[str] = set()
    for game in registry.get("games", []):
        slug = str(game.get("slug", ""))
        if not is_safe_slug(slug):
            continue
        raw_title = clean_title(str(game.get("title", slug)))[:80]
        raw_who = str(game.get("requested_by", "a viewer"))[:60]
        title = html.escape(raw_title)
        who = html.escape(raw_who)
        builder = str(game.get("built_by") or "").strip().title()[:20]
        if builder:
            builders.add(builder.lower())
        search = html.escape(f"{raw_title} {raw_who} {builder}".lower(), quote=True)
        try:
            when = int(float(game.get("published_at") or 0))
        except (TypeError, ValueError):
            when = 0
        thumb = (
            f'<img src="games/{slug}/thumb.png" alt="" loading="lazy">'
            if game.get("has_thumbnail")
            else '<div class="ph" aria-hidden="true"></div>'
        )
        chip = (
            f'<span class="chip {html.escape(builder.lower())}">Built by {html.escape(builder)}</span>'
            if builder
            else ""
        )
        cards.append(
            f'<article class="card" data-search="{search}" data-builder="{html.escape(builder.lower())}">'
            f'<a class="play" href="games/{slug}/" aria-label="Play {html.escape(raw_title, quote=True)}">'
            f'<div class="shot">{thumb}<span class="go">▶ Play</span></div>'
            f'<div class="info">{chip}<h2>{title}</h2>'
            f'<p class="who">requested by <b>{who}</b>'
            f'{f" · <time data-t={when}></time>" if when else ""}</p></div></a>'
            f'<button class="share" type="button" data-slug="{slug}" '
            f'data-title="{html.escape(raw_title, quote=True)}" title="Share with friends">'
            '<span>Share with friends</span></button>'
            "</article>"
        )
    count = len(cards)
    people = len(
        {
            str(g.get("requested_by", "")).lower()
            for g in registry.get("games", [])
            if g.get("requested_by")
        }
    )
    body = (
        "\n".join(cards)
        or '<p class="empty">Nothing published yet. Ask Mika or Luna on stream!</p>'
    )
    live = _safe_url(live_url)
    live_button = (
        f'<a class="live" href="{html.escape(live, quote=True)}" target="_blank" '
        'rel="noopener"><i></i>Watch<span class="x"> Mika and Luna</span> live</a>'
        if live
        else ""
    )
    ask = (
        f'<a class="cta" href="{html.escape(live, quote=True)}" target="_blank" rel="noopener">'
        "Ask for your own game in the live chat</a>"
        if live
        else ""
    )
    admin = html.escape(str(admin_url or ""), quote=True)
    admin_link = f'<a class="admin" href="{admin}" rel="nofollow">Admin</a>' if admin else ""
    filters = "".join(
        f'<button type="button" class="f" data-f="{b}">{b.title()}</button>'
        for b in sorted(builders)
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Games built live by Mika and Luna</title>
<meta name="description" content="Games requested in chat and coded live on stream by Mika and Luna.">
<meta property="og:title" content="Games built live by Mika and Luna">
<meta property="og:description" content="Every game here was requested in chat and coded live on stream. Find yours and play it.">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,800&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #070b18; --panel: #0f1628; --panel-2: #141d33; --line: rgba(160, 185, 255, .12);
    --text: #eef2ff; --muted: #94a3c4; --lamp: #ffb55e; --rose: #ff6f9c; --sky: #9cc2ff; --mint: #5fe3c8;
  }}
  * {{ box-sizing: border-box; }}
  html {{ scroll-behavior: smooth; }}
  body {{ margin: 0; min-height: 100vh; color: var(--text); background: var(--bg);
         font: 16px/1.55 Inter, "Segoe UI", system-ui, sans-serif; -webkit-font-smoothing: antialiased; }}
  body::before {{ content: ""; position: fixed; inset: 0; z-index: -1; pointer-events: none;
    background:
      radial-gradient(900px 520px at 12% -8%, rgba(255, 111, 156, .16), transparent 60%),
      radial-gradient(800px 560px at 92% 4%, rgba(156, 194, 255, .16), transparent 60%),
      radial-gradient(700px 500px at 50% 110%, rgba(95, 227, 200, .08), transparent 60%); }}
  a {{ color: inherit; }}
  .wrap {{ max-width: 1180px; margin: 0 auto; padding: 0 20px; }}
  header {{ display: flex; align-items: center; gap: 12px; padding: 22px 0; }}
  .brand {{ font: 800 17px "Bricolage Grotesque", Inter, sans-serif; letter-spacing: -.01em; text-decoration: none; }}
  .brand span {{ color: var(--lamp); }}
  header .live {{ margin-left: auto; }}
  .live {{ display: inline-flex; align-items: center; gap: 10px; padding: 11px 18px; border-radius: 999px;
          font-weight: 600; font-size: 15px; text-decoration: none; color: #1a1206;
          background: linear-gradient(135deg, #ffd08a, var(--lamp)); box-shadow: 0 10px 30px -12px rgba(255, 181, 94, .7); }}
  .live i {{ width: 9px; height: 9px; border-radius: 50%; background: #e11d48; box-shadow: 0 0 0 0 rgba(225, 29, 72, .6);
            animation: pulse 1.8s infinite; }}
  @keyframes pulse {{ 70% {{ box-shadow: 0 0 0 9px rgba(225, 29, 72, 0); }} 100% {{ box-shadow: 0 0 0 0 rgba(225, 29, 72, 0); }} }}
  .hero {{ padding: 44px 0 30px; }}
  .eyebrow {{ display: inline-block; padding: 6px 12px; border: 1px solid var(--line); border-radius: 999px;
             color: var(--muted); font-size: 13px; letter-spacing: .04em; background: rgba(255, 255, 255, .03); }}
  h1 {{ margin: 18px 0 14px; font: 800 clamp(40px, 7vw, 82px)/1 "Bricolage Grotesque", Inter, sans-serif;
        letter-spacing: -.035em; max-width: 14ch; }}
  h1 em {{ font-style: normal; background: linear-gradient(100deg, var(--lamp), var(--rose) 55%, var(--sky));
           -webkit-background-clip: text; background-clip: text; color: transparent; }}
  .lead {{ margin: 0; max-width: 56ch; color: var(--muted); font-size: clamp(16px, 2vw, 19px); }}
  .stats {{ display: flex; gap: 28px; margin-top: 26px; color: var(--muted); font-size: 14px; }}
  .stats b {{ display: block; color: var(--text); font: 800 28px "Bricolage Grotesque", Inter, sans-serif; }}
  .tools {{ position: sticky; top: 0; z-index: 5; display: flex; flex-wrap: wrap; gap: 10px; align-items: center;
           padding: 14px 0; margin: 18px 0 8px; background: linear-gradient(var(--bg) 70%, transparent); }}
  .search {{ flex: 1 1 300px; display: flex; align-items: center; gap: 10px; padding: 0 16px; border-radius: 14px;
            background: var(--panel); border: 1px solid var(--line); }}
  .search:focus-within {{ border-color: rgba(255, 181, 94, .6); box-shadow: 0 0 0 4px rgba(255, 181, 94, .12); }}
  .search svg {{ flex: none; opacity: .6; }}
  .search input {{ flex: 1; min-width: 0; padding: 14px 0; border: 0; outline: 0; background: transparent;
                  color: var(--text); font: inherit; font-size: 16px; }}
  .f {{ padding: 11px 16px; border-radius: 12px; border: 1px solid var(--line); background: var(--panel);
       color: var(--muted); font: 600 14px Inter, sans-serif; cursor: pointer; }}
  .f[aria-pressed="true"] {{ color: var(--text); border-color: rgba(255, 181, 94, .55); background: var(--panel-2); }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 18px; padding-bottom: 20px; }}
  .card {{ position: relative; display: flex; flex-direction: column; border-radius: 18px; overflow: hidden;
          background: var(--panel); border: 1px solid var(--line);
          transition: transform .25s ease, border-color .25s ease, box-shadow .25s ease; }}
  .card:hover, .card:focus-within {{ transform: translateY(-4px); border-color: rgba(255, 181, 94, .45);
          box-shadow: 0 24px 50px -24px rgba(0, 0, 0, .8), 0 0 0 1px rgba(255, 181, 94, .12); }}
  .play {{ display: flex; flex-direction: column; flex: 1; text-decoration: none; }}
  .shot {{ position: relative; aspect-ratio: 16 / 9; overflow: hidden; background: #0b1328; }}
  .shot img, .shot .ph {{ display: block; width: 100%; height: 100%; object-fit: cover; transition: transform .5s ease; }}
  .shot .ph {{ background: radial-gradient(circle at 30% 30%, rgba(255, 181, 94, .35), transparent 50%),
                           radial-gradient(circle at 75% 70%, rgba(156, 194, 255, .3), transparent 55%), #0f1a35; }}
  .shot img {{ animation: drift 14s ease-in-out infinite alternate; }}
  .card:nth-child(2n) .shot img {{ animation-delay: -5s; }}
  .card:nth-child(3n) .shot img {{ animation-delay: -9s; }}
  @keyframes drift {{ from {{ transform: scale(1.02) translate(0, 0); }} to {{ transform: scale(1.12) translate(-2%, -1.5%); }} }}
  .shot iframe {{ position: absolute; inset: 0; z-index: 1; width: 100%; height: 100%; border: 0;
                 pointer-events: none; opacity: 0; transition: opacity .35s ease; background: #0b1328; }}
  .shot iframe.on {{ opacity: 1; }}
  .shot::after {{ content: ""; position: absolute; inset: 0; background: linear-gradient(transparent 55%, rgba(7, 11, 24, .75)); }}
  .go {{ position: absolute; z-index: 1; right: 12px; bottom: 12px; padding: 7px 13px; border-radius: 999px;
        font-weight: 600; font-size: 13px; color: #1a1206; background: var(--lamp);
        opacity: 0; transform: translateY(6px); transition: .25s ease; }}
  .card:hover .go, .card:focus-within .go {{ opacity: 1; transform: none; }}
  .info {{ padding: 14px 16px 6px; }}
  .chip {{ display: inline-block; margin-bottom: 8px; padding: 3px 9px; border-radius: 999px; font-size: 12px; font-weight: 600;
          color: var(--sky); background: rgba(156, 194, 255, .1); border: 1px solid rgba(156, 194, 255, .25); }}
  .chip.mika {{ color: var(--lamp); background: rgba(255, 181, 94, .1); border-color: rgba(255, 181, 94, .3); }}
  .card h2 {{ margin: 0 0 6px; font: 700 19px/1.25 "Bricolage Grotesque", Inter, sans-serif; letter-spacing: -.01em;
             display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }}
  .who {{ margin: 0; color: var(--muted); font-size: 14px; }}
  .who b {{ color: var(--text); font-weight: 600; }}
  .share {{ display: flex; align-items: center; justify-content: center; gap: 8px; margin: 10px 16px 16px; padding: 10px;
           border-radius: 12px; border: 1px solid var(--line); background: rgba(255, 255, 255, .03);
           color: var(--text); font: 600 14px Inter, sans-serif; cursor: pointer; transition: background .2s, border-color .2s; }}
  .share:hover {{ background: rgba(255, 111, 156, .12); border-color: rgba(255, 111, 156, .45); }}
  .empty, .none {{ color: var(--muted); text-align: center; padding: 40px 0; }}
  .none {{ display: none; }}
  .ask {{ margin: 40px 0 0; padding: 34px; border-radius: 22px; text-align: center; border: 1px solid var(--line);
         background: linear-gradient(135deg, rgba(255, 111, 156, .1), rgba(156, 194, 255, .08)); }}
  .ask h3 {{ margin: 0 0 8px; font: 800 clamp(24px, 4vw, 34px) "Bricolage Grotesque", Inter, sans-serif; letter-spacing: -.02em; }}
  .ask p {{ margin: 0 auto 18px; max-width: 48ch; color: var(--muted); }}
  .cta {{ display: inline-block; padding: 12px 20px; border-radius: 12px; font-weight: 600; text-decoration: none;
         color: #1a1206; background: var(--lamp); }}
  footer {{ display: flex; flex-wrap: wrap; gap: 12px; justify-content: space-between; padding: 34px 0 40px; color: var(--muted); font-size: 13px; }}
  .admin {{ color: rgba(148, 163, 196, .45); text-decoration: none; }}
  .admin:hover {{ color: var(--muted); }}
  @media (max-width: 560px) {{ header .live {{ padding: 9px 13px; font-size: 13px; }} header .live .x {{ display: none; }} .stats {{ gap: 18px; }} .ask {{ padding: 24px 18px; }} }}
  @media (prefers-reduced-motion: reduce) {{ * {{ transition: none !important; animation: none !important; }} }}
</style>
</head>
<body>
<div class="wrap">
<header>
  <a class="brand" href="./">Selwyn Builds <span>·</span> Mika &amp; Luna</a>
  {live_button}
</header>
<section class="hero">
  <span class="eyebrow">Coded live on YouTube, one chat message at a time</span>
  <h1>Games built live by <em>Mika and Luna</em></h1>
  <p class="lead">Every one of these was requested in chat and coded on stream. Search your username to find yours, play it, and share it with friends.</p>
  <div class="stats"><div><b>{count}</b>games and sites</div><div><b>{people}</b>viewers who asked</div></div>
</section>
<div class="tools">
  <label class="search"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
  <input id="search" type="search" placeholder="Search your name, a game, Mika or Luna…" aria-label="Search games"></label>
  <button type="button" class="f" data-f="" aria-pressed="true">All</button>{filters}
</div>
<div class="grid" id="grid">
{body}
</div>
<p class="none" id="none">No game matches that yet. Ask Mika or Luna for it on stream!</p>
<section class="ask">
  <h3>Want your own?</h3>
  <p>Type what you want in the live chat. Mika or Luna codes it on stream, and it shows up here a few minutes later.</p>
  {ask}
</section>
<footer><span>Made live on stream by Mika and Luna for Selwyn Builds</span>{admin_link}</footer>
</div>
<script>
(function () {{
  var search = document.getElementById("search");
  var cards = Array.prototype.slice.call(document.querySelectorAll(".card"));
  var none = document.getElementById("none");
  var builder = "";
  function filter() {{
    var words = search.value.toLowerCase().replace(/^@/, "").split(/\\s+/).filter(Boolean);
    var shown = 0;
    cards.forEach(function (card) {{
      var text = card.getAttribute("data-search") || "";
      var ok = words.every(function (w) {{ return text.indexOf(w) !== -1; }})
        && (!builder || card.getAttribute("data-builder") === builder);
      card.style.display = ok ? "" : "none";
      if (ok) shown++;
    }});
    none.style.display = cards.length && !shown ? "block" : "none";
  }}
  search.addEventListener("input", filter);
  Array.prototype.forEach.call(document.querySelectorAll(".f"), function (b) {{
    b.addEventListener("click", function () {{
      builder = b.getAttribute("data-f") || "";
      Array.prototype.forEach.call(document.querySelectorAll(".f"), function (x) {{
        x.setAttribute("aria-pressed", x === b ? "true" : "false");
      }});
      filter();
    }});
  }});
  var q = new URLSearchParams(location.search).get("q");
  if (q) {{ search.value = q; filter(); }}

  // Hover a card: the real game plays inside it (the AI plays, ?demo).
  // Phones: the card in the middle of the screen plays.
  var live = null;
  function play(card) {{
    if (live && live.card === card) return;
    stop();
    var link = card.querySelector(".play"), shot = card.querySelector(".shot");
    if (!link || !shot) return;
    var frame = document.createElement("iframe");
    frame.src = link.getAttribute("href") + "?demo=1";
    frame.setAttribute("tabindex", "-1");
    frame.setAttribute("aria-hidden", "true");
    frame.setAttribute("loading", "lazy");
    frame.onload = function () {{ setTimeout(function () {{ frame.classList.add("on"); }}, 350); }};
    shot.appendChild(frame);
    live = {{ card: card, frame: frame }};
  }}
  function stop() {{
    if (!live) return;
    var f = live.frame; live = null;
    f.classList.remove("on");
    setTimeout(function () {{ f.remove(); }}, 350);
  }}
  var hover = window.matchMedia && matchMedia("(hover: hover)").matches;
  cards.forEach(function (card) {{
    if (hover) {{
      card.addEventListener("mouseenter", function () {{ play(card); }});
      card.addEventListener("mouseleave", function () {{ if (live && live.card === card) stop(); }});
    }}
  }});
  if (!hover && "IntersectionObserver" in window) {{
    var seen = new IntersectionObserver(function (entries) {{
      entries.forEach(function (e) {{
        if (e.isIntersecting && e.intersectionRatio > 0.8) play(e.target);
        else if (live && live.card === e.target) stop();
      }});
    }}, {{ threshold: [0, 0.8] }});
    cards.forEach(function (card) {{ seen.observe(card); }});
  }}

  var now = Date.now() / 1000;
  Array.prototype.forEach.call(document.querySelectorAll("time[data-t]"), function (t) {{
    var s = now - Number(t.getAttribute("data-t"));
    var d = Math.floor(s / 86400), h = Math.floor(s / 3600), m = Math.floor(s / 60);
    t.textContent = d > 0 ? d + (d === 1 ? " day ago" : " days ago") : h > 0 ? h + "h ago" : m > 1 ? m + " min ago" : "just now";
  }});

  document.addEventListener("click", function (event) {{
    var button = event.target.closest(".share");
    if (!button) return;
    var url = new URL("games/" + button.getAttribute("data-slug") + "/", location.href).href;
    var title = button.getAttribute("data-title") || "A game built live by Mika and Luna";
    var text = "Play " + title + ", built live on stream by Mika and Luna!";
    if (navigator.share) {{
      navigator.share({{ title: title, text: text, url: url }}).catch(function () {{}});
      return;
    }}
    var label = button.querySelector("span");
    var done = function () {{
      var old = label.textContent;
      label.textContent = "Link copied!";
      setTimeout(function () {{ label.textContent = old; }}, 1600);
    }};
    if (navigator.clipboard) navigator.clipboard.writeText(url).then(done, function () {{ prompt("Copy this link", url); }});
    else prompt("Copy this link", url);
  }});
}})();
</script>
</body>
</html>
"""
