"""Turn an approved Stage program into a static folder for GitHub Pages.

    games/<slug>/index.html     the program, with Stage URLs made relative
    libs/<name>/<version>/...   only the pinned libraries it uses
    assets/<group>/<file>       only the approved art it uses

Every referenced file must resolve to a real file inside frontend/stage-libs
or frontend/stage-assets (no symlinks, no "..", size-limited). The published
page also carries a Content-Security-Policy so a generated game cannot load
trackers or scripts from anywhere else for the people who play it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .provenance import is_safe_slug

ROOT = Path(__file__).resolve().parents[3]
FRONTEND = ROOT / "frontend"
ALLOWED = {
    "/stage-libs/": ("libs", FRONTEND / "stage-libs"),
    "/stage-assets/": ("assets", FRONTEND / "stage-assets"),
}
STAGE_URL = re.compile(r"/stage-(?:libs|assets)/[A-Za-z0-9_./-]+")
EXTERNAL = re.compile(r"""(?:src|href)\s*=\s*["']\s*(?:https?:)?//""", re.I)

MAX_PAGE_BYTES = 400_000
MAX_FILE_BYTES = 3_000_000
MAX_BUNDLE_BYTES = 20_000_000

PUBLISHED_POLICY = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval' blob:; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' data: blob:; "
    "connect-src 'self'; font-src 'self' data:; worker-src 'self' blob:; object-src 'none'; "
    "base-uri 'none'; form-action 'none'"
)


class BundleError(ValueError):
    """The program cannot be published as it is."""


@dataclass
class Bundle:
    slug: str
    files: dict[str, bytes] = field(default_factory=dict)  # repo path -> bytes

    @property
    def size(self) -> int:
        return sum(len(v) for v in self.files.values())


def _resolve_stage_file(url: str) -> tuple[str, Path]:
    for prefix, (repo_dir, base) in ALLOWED.items():
        if url.startswith(prefix):
            rel = url[len(prefix) :]
            if not rel or ".." in rel.split("/") or rel.startswith("/"):
                raise BundleError(f"unsafe file path {url}")
            path = base / rel
            if path.is_symlink():
                raise BundleError(f"symlinks are not published: {url}")
            resolved = path.resolve()
            if base.resolve() not in resolved.parents or not resolved.is_file():
                raise BundleError(f"not an approved file: {url}")
            return f"{repo_dir}/{rel}", resolved
    raise BundleError(f"not an approved location: {url}")


def _library_folder_files(repo_path: str, resolved: Path) -> dict[str, Path]:
    """A library is published with its whole pinned folder (ES modules import
    siblings, e.g. three.module.js imports ./three.core.js) and its license."""
    if not repo_path.startswith("libs/"):
        return {repo_path: resolved}
    folder = resolved.parent
    base = repo_path.rsplit("/", 1)[0]
    return {
        f"{base}/{item.name}": item
        for item in sorted(folder.iterdir())
        if item.is_file() and not item.is_symlink() and not item.name.endswith(".map")
    }


# The published copy is played by a person: games read this and turn on their
# keyboard, mouse and touch controls (the Stage never sets it, so there the AI
# plays). saved_code() strips it again when a game is reopened.
# ?demo in the link (the gallery's moving previews) keeps the AI playing.
PLAYER_MODE_TAG = (
    '<script>window.GAME_MODE=/[?&]demo\\b/.test(location.search)?"stream":"player";</script>'
)
OLD_PLAYER_MODE_TAG = '<script>window.GAME_MODE="player";</script>'

# Published pages get a safety net for buttons and links the program left
# dead: they scroll to the section they name (Contact, Work...) or show a
# friendly "this is a demo" note. Nothing changes for buttons that already
# work, and the Stage copy never has this (viewers there cannot click), so
# the live build is never slowed down by it.
RESCUE_HEAD = (
    "<!--vr-rescue--><script>(function(){var a=EventTarget.prototype.addEventListener;"
    "EventTarget.prototype.addEventListener=function(t,f,o){if(/^(click|pointerdown|pointerup|"
    "mousedown|mouseup|touchstart|touchend|submit)$/.test(t)){try{this.__vrClick=true}catch(e){}}"
    "return a.call(this,t,f,o)};})();</script><!--/vr-rescue-->"
)
RESCUE_BODY = """<!--vr-rescue--><script>
window.addEventListener("load", function () { setTimeout(function () {
  if (window.__vrClick || document.__vrClick || window.onclick || document.onclick) return;
  function handled(el) {
    for (var n = el; n && n !== document; n = n.parentNode)
      if (n.__vrClick || n.onclick || (n.getAttribute && n.getAttribute("onclick"))) return true;
    return false;
  }
  var words = ["contact", "work", "project", "portfolio", "service", "pricing", "price", "plan",
    "about", "team", "feature", "faq", "menu", "book", "start", "home"];
  var sections = Array.prototype.filter.call(document.querySelectorAll("[id]"), function (e) {
    return e.offsetHeight > 60 && e.tagName !== "svg" && !(e.closest && e.closest("svg"));
  });
  function sectionFor(label) {
    label = (label || "").toLowerCase();
    for (var i = 0; i < words.length; i++) {
      if (label.indexOf(words[i]) === -1) continue;
      var w = words[i] === "book" || words[i] === "start" ? "contact" : words[i];
      for (var j = 0; j < sections.length; j++) {
        var s = sections[j], h = s.querySelector("h1,h2,h3");
        if (s.id.toLowerCase().indexOf(w) !== -1 || (h && h.textContent.toLowerCase().indexOf(w) !== -1)) return s;
      }
    }
    return null;
  }
  var note;
  function demo() {
    if (!note) {
      note = document.createElement("div");
      note.textContent = "Thanks! This page was built live on stream by Mika and Luna, so this button is a demo.";
      note.style.cssText = "position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:99999;" +
        "max-width:90vw;padding:12px 18px;border-radius:12px;background:#111827;color:#fff;" +
        "font:600 15px system-ui,sans-serif;box-shadow:0 10px 30px rgba(0,0,0,.35);transition:opacity .3s";
      document.body.appendChild(note);
    }
    note.style.opacity = "1";
    clearTimeout(note.t); note.t = setTimeout(function () { note.style.opacity = "0"; }, 2600);
  }
  function rescue(el) {
    el.addEventListener("click", function (e) {
      e.preventDefault();
      var s = sectionFor(el.textContent);
      if (s) s.scrollIntoView({ behavior: "smooth", block: "start" }); else demo();
    });
  }
  Array.prototype.forEach.call(document.querySelectorAll("a"), function (a) {
    var href = (a.getAttribute("href") || "").trim();
    if (href.charAt(0) === "#" && href.length > 1) {
      var id = href.slice(1); try { id = decodeURIComponent(id); } catch (e) {}
      if (document.getElementById(id)) return;
    } else if (href && href !== "#" && !/^javascript:/i.test(href)) return;
    if (!handled(a)) rescue(a);
  });
  Array.prototype.forEach.call(document.querySelectorAll("form"), function (f) {
    if (!handled(f) && !f.onsubmit) f.addEventListener("submit", function (e) { e.preventDefault(); demo(); });
  });
  Array.prototype.forEach.call(document.querySelectorAll("button, [role=button], input[type=button]"), function (b) {
    if (b.disabled || b.form || handled(b)) return;
    rescue(b);
  });
}, 300); });
</script><!--/vr-rescue-->"""


def strip_published_extras(page: str) -> str:
    """The Stage copy of a published page: no player switch, no safety net."""
    page = page.replace(PLAYER_MODE_TAG, "", 1).replace(OLD_PLAYER_MODE_TAG, "", 1)
    return re.sub(r"<!--vr-rescue-->.*?<!--/vr-rescue-->", "", page, flags=re.S)


def _add_rescue(html: str) -> str:
    match = re.search(r"</body\s*>", html, re.I)
    if match:
        return html[: match.start()] + RESCUE_BODY + html[match.start() :]
    return html + RESCUE_BODY


def _add_policy(html: str) -> str:
    html = _add_rescue(html)
    meta = (
        f'<meta http-equiv="Content-Security-Policy" content="{PUBLISHED_POLICY}">'
        + PLAYER_MODE_TAG
        + RESCUE_HEAD
    )
    match = re.search(r"<head(\s[^>]*)?>", html, re.I)
    if match:
        return html[: match.end()] + meta + html[match.end() :]
    return meta + html


def build_bundle(source: str, slug: str) -> Bundle:
    if not is_safe_slug(slug):
        raise BundleError(f"unsafe slug {slug!r}")
    html = str(source or "")
    if not html.strip():
        raise BundleError("there is no program")
    if len(html.encode("utf-8")) > MAX_PAGE_BYTES:
        raise BundleError("the program is too large to publish")
    if EXTERNAL.search(html):
        raise BundleError("the program loads files from the internet")

    bundle = Bundle(slug=slug)
    for url in sorted(set(STAGE_URL.findall(html))):
        repo_path, resolved = _resolve_stage_file(url)
        for path, file in _library_folder_files(repo_path, resolved).items():
            data = file.read_bytes()
            if len(data) > MAX_FILE_BYTES:
                raise BundleError(f"{path} is too large")
            bundle.files[path] = data

    # Stage URLs become relative to games/<slug>/index.html.
    page = html.replace("/stage-libs/", "../../libs/").replace(
        "/stage-assets/", "../../assets/"
    )
    bundle.files[f"games/{slug}/index.html"] = _add_policy(page).encode("utf-8")
    if bundle.size > MAX_BUNDLE_BYTES:
        raise BundleError("the published folder would be too large")
    return bundle
