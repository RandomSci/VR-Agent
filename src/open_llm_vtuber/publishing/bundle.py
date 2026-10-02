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


def _add_policy(html: str) -> str:
    meta = f'<meta http-equiv="Content-Security-Policy" content="{PUBLISHED_POLICY}">'
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
