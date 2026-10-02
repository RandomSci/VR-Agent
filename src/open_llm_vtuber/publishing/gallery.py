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


def render_gallery(registry: dict[str, Any], live_url: str = "") -> str:
    """The gallery: search by title or name, share any game, watch the stream."""
    cards = []
    for game in registry.get("games", []):
        slug = str(game.get("slug", ""))
        if not is_safe_slug(slug):
            continue
        raw_title = clean_title(str(game.get("title", slug)))[:80]
        raw_who = str(game.get("requested_by", "a viewer"))[:60]
        title = html.escape(raw_title)
        who = html.escape(raw_who)
        builder = str(game.get("built_by") or "").strip().title()[:20]
        search = html.escape(f"{raw_title} {raw_who} {builder}".lower(), quote=True)
        credit = (
            f"Built by {html.escape(builder)} · requested by {who}"
            if builder
            else f"Requested by {who}"
        )
        thumb = (
            f'<img src="games/{slug}/thumb.png" alt="" loading="lazy">'
            if game.get("has_thumbnail")
            else '<div class="ph"></div>'
        )
        cards.append(
            f'<article class="card" data-search="{search}">'
            f'<a class="play" href="games/{slug}/">{thumb}'
            f"<h2>{title}</h2><p>{credit}</p></a>"
            f'<button class="share" type="button" data-slug="{slug}" '
            f'data-title="{html.escape(raw_title, quote=True)}">Share with friends</button>'
            "</article>"
        )
    body = (
        "\n".join(cards)
        or '<p class="empty">Nothing published yet. Ask Mika or Luna on stream!</p>'
    )
    live = _safe_url(live_url)
    live_button = (
        f'<a class="live" href="{html.escape(live, quote=True)}" target="_blank" '
        'rel="noopener">▶ Watch Mika and Luna live</a>'
        if live
        else ""
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Games built live by Mika and Luna</title>
<meta name="description" content="Games requested in chat and coded live on stream by Mika and Luna.">
<style>
  :root {{ --night: #0f1d3a; --dusk: #2a4a86; --glass: #9cc2ff; --lamp: #ffb55e; --paper: #f4ead8; --paint: #ff6f9c; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; min-height: 100vh; color: var(--paper);
         background: linear-gradient(#0b1530, #1b2f5c);
         font-family: Nunito, Quicksand, "Avenir Next", "Segoe UI", system-ui, sans-serif; }}
  main {{ max-width: 1100px; margin: 0 auto; padding: 56px 20px; }}
  h1 {{ margin: 0 0 8px; font-size: clamp(36px, 6vw, 64px); font-weight: 900;
        text-shadow: 0 0 28px rgba(255, 181, 94, .45); }}
  .lead {{ margin: 0 0 24px; color: var(--glass); font-size: 20px; }}
  .bar {{ display: flex; gap: 12px; flex-wrap: wrap; align-items: center; margin-bottom: 32px; }}
  .search {{ flex: 1 1 280px; padding: 14px 18px; border-radius: 999px; font: inherit; font-size: 18px;
            color: var(--paper); background: rgba(15, 29, 58, .7); border: 1px solid rgba(156, 194, 255, .35); }}
  .search:focus {{ outline: none; border-color: var(--lamp); }}
  .live {{ padding: 14px 22px; border-radius: 999px; font-weight: 800; text-decoration: none;
          color: var(--night); background: var(--lamp); white-space: nowrap; }}
  .live:hover {{ filter: brightness(1.08); }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 20px; }}
  .card {{ display: flex; flex-direction: column; border-radius: 18px; overflow: hidden;
          background: rgba(42, 74, 134, .35); border: 1px solid rgba(156, 194, 255, .2); }}
  .card:hover, .card:focus-within {{ border-color: var(--lamp); }}
  .play {{ display: block; text-decoration: none; color: inherit; flex: 1; }}
  .play img, .play .ph {{ display: block; width: 100%; aspect-ratio: 16 / 9; object-fit: cover;
                         background: linear-gradient(135deg, #13254a, #2a4a86); }}
  .card h2 {{ margin: 14px 16px 4px; font-size: 22px; }}
  .card p {{ margin: 0 16px 12px; color: rgba(244, 234, 216, .7); }}
  .share {{ margin: 0 16px 16px; padding: 10px 14px; border-radius: 12px; font: inherit; font-weight: 800;
           cursor: pointer; color: var(--paper); background: rgba(255, 111, 156, .18);
           border: 1px solid rgba(255, 111, 156, .45); }}
  .share:hover {{ background: rgba(255, 111, 156, .3); }}
  .empty, .none {{ color: var(--glass); }}
  .none {{ display: none; }}
</style>
</head>
<body>
<main>
<h1>Games built live by Mika and Luna</h1>
<p class="lead">Every one of these was requested in chat and coded on stream. Play them, share them, and ask for your own.</p>
<div class="bar">
  <input class="search" id="search" type="search" placeholder="Search your name, a game, Mika or Luna…" aria-label="Search games">
  {live_button}
</div>
<div class="grid" id="grid">
{body}
</div>
<p class="none" id="none">No game matches that yet. Ask Mika or Luna for it on stream!</p>
</main>
<script>
(function () {{
  var search = document.getElementById("search");
  var cards = Array.prototype.slice.call(document.querySelectorAll(".card"));
  var none = document.getElementById("none");
  function filter() {{
    var words = search.value.toLowerCase().replace(/^@/, "").split(/\\s+/).filter(Boolean);
    var shown = 0;
    cards.forEach(function (card) {{
      var text = card.getAttribute("data-search") || "";
      var ok = words.every(function (w) {{ return text.indexOf(w) !== -1; }});
      card.style.display = ok ? "" : "none";
      if (ok) shown++;
    }});
    none.style.display = cards.length && !shown ? "block" : "none";
  }}
  search.addEventListener("input", filter);
  var q = new URLSearchParams(location.search).get("q");
  if (q) {{ search.value = q; filter(); }}

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
    var done = function () {{
      var old = button.textContent;
      button.textContent = "Link copied!";
      setTimeout(function () {{ button.textContent = old; }}, 1600);
    }};
    if (navigator.clipboard) navigator.clipboard.writeText(url).then(done, function () {{ prompt("Copy this link", url); }});
    else prompt("Copy this link", url);
  }});
}})();
</script>
</body>
</html>
"""
