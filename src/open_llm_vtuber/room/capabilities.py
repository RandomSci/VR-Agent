"""What Mika can build with: kinds, libraries, templates and assets.

The cheap decision call picks ONE kind from a fixed list (it already reads the
viewer's comment, in any language, so no keyword matching is involved). The
backend then gives the code writer only what that kind needs:

* a short guide (which library, how it should look and behave),
* the pinned library URL,
* a working starting template for kinds that have one,
* the real asset filenames, so nothing is invented.

This is one model call with the right context injected, which is faster and
more reliable for GPT-4o-mini than letting it call tools. The discovery
functions at the bottom (list_available_assets, search_assets,
list_templates, get_template_info) are the same data in tool shape, ready to
be exposed over MCP later without changing anything here.

Files live in frontend/ and are served by the Stage server:
    /stage-libs/<name>/<version>/...   pinned third-party libraries
    /stage-assets/<group>/<file>       approved art
    /stage-templates/<name>.html       starting templates
Generated code can only reference those three prefixes (the preview's
Content-Security-Policy blocks everything else).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

FRONTEND = Path(__file__).resolve().parents[3] / "frontend"
LIBS_DIR = FRONTEND / "stage-libs"
ASSETS_DIR = FRONTEND / "stage-assets"
TEMPLATES_DIR = FRONTEND / "stage-templates"
HOST_FILE = FRONTEND.parent / "room" / "host.md"

STAGE_PREFIXES = ("/stage-libs/", "/stage-assets/", "/stage-templates/")


@dataclass(frozen=True)
class Kind:
    name: str
    language: str  # "python" | "web"
    label: str  # shown to the decision model
    guide: str  # given to the code writer
    libraries: tuple[str, ...] = ()
    template: Optional[str] = None
    uses_assets: bool = False
    tier: str = "fast"  # fast | normal | complex (timeouts and Stage text)
    python_packages: tuple[str, ...] = field(default_factory=tuple)


PY_STYLE = (
    "Stream look for charts: plt.style.use('dark_background'); figure facecolor "
    "'#0f1d3a', axes facecolor '#13254a'; figsize=(16, 9), dpi=100; colors in order "
    "'#ffb55e', '#9cc2ff', '#ff6f9c', '#5fe3c8', '#f4ead8'; title 28pt bold, labels "
    "18pt, ticks 14pt; remove top and right spines; light grid alpha 0.15."
)

KINDS: dict[str, Kind] = {
    k.name: k
    for k in [
        # ------------------------------------------------------------ Python
        Kind(
            "python_lesson",
            "python",
            "teaching plain Python: variables, lists, dicts, loops, functions, "
            "classes, algorithms and data structures, explained with prints",
            "A PLAIN PYTHON LESSON. No charts, no matplotlib, no plotting at all.\n"
            "- Teach with small steps and print() after each step, so the terminal "
            "on stream shows what happened ('After append: [...]').\n"
            "- Use friendly real-world examples (snacks, game scores, playlists).\n"
            "- Short sections with a comment header each. Standard library only.",
        ),
        Kind(
            "python_chart",
            "python",
            "a normal chart or graph with matplotlib",
            "A MATPLOTLIB CHART. " + PY_STYLE + "\n"
            "- Make up small realistic data if none was given, and label it.\n"
            "- Save once: plt.savefig('output.png', bbox_inches='tight'). Print a "
            "one-line summary.",
            python_packages=("matplotlib", "numpy"),
        ),
        Kind(
            "python_stats",
            "python",
            "a statistics chart: distributions, correlations, heatmaps, regression, "
            "comparisons between groups",
            "A STATISTICAL CHART WITH SEABORN on top of matplotlib.\n"
            "- import seaborn as sns; sns.set_theme(style='darkgrid') then apply the "
            "stream look: " + PY_STYLE + "\n"
            "- Use sns.heatmap, sns.histplot, sns.kdeplot, sns.violinplot, "
            "sns.regplot or sns.boxplot as fits. Build data with numpy or pandas.\n"
            "- Save with plt.savefig('output.png', bbox_inches='tight').",
            python_packages=("seaborn", "pandas", "numpy", "matplotlib"),
        ),
        Kind(
            "python_science",
            "python",
            "a scientific or physics graph, simulation results, publication style",
            "A SCIENTIFIC FIGURE. Compute with numpy and scipy (scipy.integrate."
            "solve_ivp for motion and ODEs, scipy.optimize, scipy.stats).\n"
            "- Style: import scienceplots; plt.style.use(['science', 'no-latex', "
            "'dark_background']) (no-latex is required, LaTeX is not installed).\n"
            "- Units on every axis, a legend, a short title. figsize=(16, 9).\n"
            "- Save with plt.savefig('output.png', dpi=100, bbox_inches='tight').",
            python_packages=("numpy", "scipy", "matplotlib", "scienceplots"),
        ),
        Kind(
            "python_math",
            "python",
            "solving or explaining maths: equations, derivatives, integrals, algebra, "
            "with a plot of the result",
            "SYMBOLIC MATHS WITH SYMPY.\n"
            "- Solve or simplify with sympy and print each step with sympy.pretty() "
            "so the terminal reads like a worked solution.\n"
            "- Plot the function with numpy (sympy.lambdify) and matplotlib in the "
            "stream look: " + PY_STYLE + " Save to output.png.",
            python_packages=("sympy", "numpy", "matplotlib"),
        ),
        Kind(
            "python_data",
            "python",
            "working with a table of data: pandas, grouping, sorting, summaries",
            "DATA WITH PANDAS.\n"
            "- Build a small realistic DataFrame in code, then show each step with "
            "print(df.head()) or print(summary.to_string()).\n"
            "- End with one chart of the result in the stream look: "
            + PY_STYLE
            + " Save to output.png.",
            python_packages=("pandas", "numpy", "matplotlib"),
        ),
        Kind(
            "python_animation",
            "python",
            "anything in Python that should MOVE, twinkle, grow or animate over time",
            "A MATPLOTLIB ANIMATION SAVED AS A GIF (a still PNG cannot move).\n"
            "- from matplotlib.animation import FuncAnimation, PillowWriter\n"
            "- fig, ax = plt.subplots(figsize=(12.8, 7.2), dpi=75) in the stream "
            "look: plt.style.use('dark_background'); fig.set_facecolor('#0f1d3a'); "
            "ax.set_facecolor('#13254a') (never a white plot area); colors "
            "'#ffb55e', '#9cc2ff', '#ff6f9c', '#5fe3c8'.\n"
            "- 36 to 60 frames, update(frame) changes the artists, blit=True.\n"
            "- anim.save('output.gif', writer=PillowWriter(fps=16)); print how many "
            "frames were saved. Keep it light: it must finish in under 10 seconds.\n"
            "- Create the line or bars ONCE, then only call set_data or "
            "set_height in update. Never clear and redraw the axes, never call "
            "seaborn, kdeplot or savefig inside update or a loop: that runs out "
            "of time and the run is killed.",
            python_packages=("matplotlib", "numpy", "pillow"),
            tier="normal",
        ),
        Kind(
            "python_image",
            "python",
            "generating or editing a picture with code (patterns, gradients, pixel art)",
            "AN IMAGE WITH PILLOW.\n"
            "- from PIL import Image, ImageDraw, ImageFilter; draw on a 1280x720 "
            "canvas in the stream colors ('#0f1d3a', '#ffb55e', '#9cc2ff', '#ff6f9c', "
            "'#5fe3c8').\n"
            "- Save as output.png and print what was drawn.",
            python_packages=("pillow", "numpy"),
        ),
        # --------------------------------------------------------------- Web
        Kind(
            "web_game_flyer",
            "web",
            "a side-scrolling game where things come from the right: Flappy Bird, "
            "endless runner, dodge-the-obstacles flyer",
            "A PHASER 3 SIDE-SCROLLER, built from the starting template.\n"
            "- Keep the template's structure: the SETTINGS block, preload, create, "
            "update, the autopilot that plays the game, the PLAYER CONTROLS block "
            "(tap, click, Space) with its if (PLAYER) checks, score and restart.\n"
            "- Change art, speed and theme through SETTINGS and the asset list. "
            "To make a character the player use /stage-assets/characters/luna-head.png "
            "or mika-head.png.\n"
            "- The autopilot must keep playing well enough to be fun to watch.",
            libraries=("phaser",),
            template="game-flyer",
            uses_assets=True,
            tier="normal",
        ),
        Kind(
            "web_game_shooter",
            "web",
            "a space or arena game: shooter, asteroids, dodge and collect, enemies "
            "attacking the player",
            "A PHASER 3 SHOOTER, built from the starting template.\n"
            "- Keep the template's structure: SETTINGS, preload, create, update, the "
            "autopilot pilot, the PLAYER CONTROLS block (mouse, touch, arrow keys) "
            "with its if (PLAYER) checks, waves, explosions, lives and score.\n"
            "- Hits use distance: a shot hits an enemy only when "
            "Phaser.Math.Distance.Between(shot, enemy) < enemy radius + 12, "
            "like the template. Never count a shot as a hit just because it is "
            "above the enemy.\n"
            "- Swap sprites and tune numbers to match the request. Add new enemy "
            "types by copying the existing spawn pattern.",
            libraries=("phaser",),
            template="game-shooter",
            uses_assets=True,
            tier="normal",
        ),
        Kind(
            "web_game",
            "web",
            "any other small 2D game: pong, snake, breakout, platformer, puzzle, "
            "card or board game",
            "A SMALL 2D GAME WITH PHASER 3 (global Phaser), built from the "
            "starting template (an arena where AI players chase stars and dodge "
            "slimes).\n"
            "- Keep the template's skeleton: SETTINGS, preload, create, the AI "
            "steer() that moves every player by itself, control() and the PLAYER "
            "CONTROLS block that let a person play the first player when "
            "published, update(dt), the score "
            "labels, rounds that restart on their own, particles and tweens.\n"
            "- Turn it into the requested game by changing the rules, sprites and "
            "numbers. For Mika or Luna as characters use "
            "/stage-assets/characters/mika-head.png and luna-head.png.\n"
            "- Move things yourself with x, y and dt like the template (no arcade "
            "physics needed). Load art with this.load.svg(key, url, {width, "
            "height}) or this.load.image(key, url) for PNG.\n"
            "- Grid games (snake, tetris-like, mazes): draw the board as a grid "
            "of cells with this.add.graphics(), move one cell per step on a "
            "timer (every 100 to 150 ms), and let the AI pick the next cell. "
            "A snake is a chain of cells that grows when it eats food.\n"
            "- Make it look like the requested game, not like the template: "
            "replace the template's title, sprites and background theme.",
            libraries=("phaser",),
            template="game-arena",
            uses_assets=True,
            tier="normal",
        ),
        Kind(
            "web_creative",
            "web",
            "generative art, particles, flow fields, patterns, mathematical or "
            "creative animation in 2D",
            "CREATIVE CODING WITH P5.JS (global mode), built from the starting template.\n"
            "- setup() creates createCanvas(windowWidth, windowHeight); draw() runs "
            "every frame; windowResized() resizes.\n"
            "- Additive glow: blendMode(ADD) for light, translucent background for "
            "trails. Use noise() for organic motion. Keep particle counts sane "
            "(under about 1500).",
            libraries=("p5",),
            template="creative-p5",
            uses_assets=True,
        ),
        Kind(
            "web_3d",
            "web",
            "a 3D scene: planets, solar system, galaxy, 3D shapes, space, terrain",
            "A THREE.JS 3D SCENE, built from the starting template.\n"
            "- It is an ES module: keep the import map and "
            "<script type=\"module\"> import * as THREE from 'three'.\n"
            "- Keep the renderer, resize handling, slow camera orbit and lights from "
            "the template; add meshes, materials (MeshStandardMaterial with "
            "emissive for glow) and Points for stars or particles.\n"
            "- No addons and no model files: build everything from geometries.",
            libraries=("three",),
            template="scene-3d",
            uses_assets=True,
            tier="complex",
        ),
        Kind(
            "web_physics",
            "web",
            "a physics simulation: falling and stacking objects, collisions, "
            "gravity, bouncing, ragdolls, Newton's cradle",
            "A MATTER.JS PHYSICS SCENE, built from the starting template.\n"
            "- Keep the engine, the custom canvas drawing loop and the spawner.\n"
            "- Bodies: Matter.Bodies.rectangle, circle, polygon; constraints with "
            "Matter.Constraint. Restitution and friction make it feel right.",
            libraries=("matter",),
            template="physics-matter",
            uses_assets=True,
        ),
        Kind(
            "web_chart",
            "web",
            "an animated chart or dashboard in the browser",
            "AN ANIMATED CHART.JS DASHBOARD, built from the starting template.\n"
            "- Keep the dark theme defaults and the timer that updates the data "
            "every few seconds so the chart keeps moving.\n"
            "- Chart types: 'line', 'bar', 'doughnut', 'radar', 'polarArea'.",
            libraries=("chartjs",),
            template="dashboard-chart",
        ),
        Kind(
            "web_site",
            "web",
            "a full website: a business, brand, portfolio or product site with "
            "several sections (menu, hero, services, work, contact). Choose this "
            "for any request for a website",
            "A MULTI-SECTION WEBSITE in plain HTML, CSS and JS (no library), built "
            "from the starting template.\n"
            "- Keep the template's machinery exactly: the sticky nav, the "
            "data-tour sections, the .reveal fade-ins, the SETTINGS block and the "
            "tour() that scrolls through every section and loops, and the stream "
            "cursor that hovers [data-hover] buttons and cards (CSS uses both "
            ":hover and .hover).\n"
            "- Change the brand, words, colours and section content to fit the "
            "request. Four to six sections, each with a clear heading. Cards in "
            "grids of 3 or 4. Real, specific copy (no lorem ipsum).\n"
            "- It is responsive: grids use auto-fit/minmax and sizes use clamp(), "
            "so it also works on a phone when published.",
            template="site-scroll",
            uses_assets=True,
            tier="normal",
        ),
        Kind(
            "web_page",
            "web",
            "ONE screen only: a poster, profile card, menu board, single card or "
            "small UI design (not a full website)",
            "A SINGLE-SCREEN WEB PAGE in plain HTML and CSS (no library), built from "
            "the starting template.\n"
            "- Everything must fit in one 16:9 screen with no scrolling: keep the "
            "template's two-column layout, at most 4 items, short text. If it "
            "does not fit, use fewer items or smaller type, never more height.\n"
            "- Motion: one orchestrated entrance, then a gentle loop that "
            "highlights one item at a time with setTimeout every 2 to 3 seconds "
            "(never switch items inside requestAnimationFrame, that flickers).",
            template="landing-page",
            uses_assets=True,
        ),
        Kind(
            "web_canvas",
            "web",
            "any other animation or visual in the browser that fits none of the above",
            "A PLAIN CANVAS ANIMATION (no library), using the skeleton in the design kit.",
        ),
    ]
}

DEFAULT_KIND = {"python": "python_lesson", "web": "web_canvas"}


def kind_for(name: str, language: str = "") -> Kind:
    """The named kind, or a safe default for the language."""
    kind = KINDS.get(str(name or "").strip().lower())
    if kind is not None:
        return kind
    return KINDS[DEFAULT_KIND.get(language, "python_lesson")]


def kinds_for_decision() -> str:
    """The kind menu shown to the decision model, grouped by language."""
    lines = []
    for language in ("python", "web"):
        for kind in KINDS.values():
            if kind.language == language:
                lines.append(f"  {kind.name:<18} {kind.label}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# libraries
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def library_manifest() -> dict[str, Any]:
    path = LIBS_DIR / "manifest.json"
    if not path.is_file():
        return {"libraries": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def library(name: str) -> Optional[dict[str, Any]]:
    return library_manifest().get("libraries", {}).get(name)


def library_lines(names: tuple[str, ...]) -> str:
    out = []
    for name in names:
        lib = library(name)
        if not lib:
            continue
        if lib.get("type") == "module":
            out.append(
                f'- {name} {lib["version"]}: ES module. <script type="importmap">'
                f'{{"imports":{{"three":"{lib["url"]}"}}}}</script> then '
                "import * as THREE from 'three' inside <script type=\"module\">."
            )
        else:
            out.append(
                f'- {name} {lib["version"]}: <script src="{lib["url"]}"></script> '
                f"(global {lib['global']})"
            )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# assets
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def asset_catalog() -> list[dict[str, Any]]:
    path = ASSETS_DIR / "catalog.json"
    if not path.is_file():
        return []
    return list(json.loads(path.read_text(encoding="utf-8")).get("assets", []))


def asset_lines() -> str:
    """Every approved asset, one line each: the model may use only these."""
    return "\n".join(
        f"- {a['url']}  {a['size'][0]}x{a['size'][1]}  {a['about']}"
        for a in asset_catalog()
    )


def list_available_assets(group: str = "") -> list[dict[str, Any]]:
    """Tool shape: every approved asset, optionally one group (sprites, ...)."""
    return [a for a in asset_catalog() if not group or a["kind"] == group]


def search_assets(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Tool shape: assets ranked by how many query words match their tags.

    For developers and future tool use. The live pipeline never routes viewer
    text through this; the decision model chooses the kind.
    """
    words = {w for w in str(query or "").lower().split() if w}
    scored = []
    for asset in asset_catalog():
        hay = set(asset["tags"]) | set(asset["about"].lower().split()) | {asset["key"]}
        score = len(words & hay)
        if score:
            scored.append((score, asset))
    scored.sort(key=lambda item: -item[0])
    return [a for _, a in scored[:limit]]


# ---------------------------------------------------------------------------
# templates
# ---------------------------------------------------------------------------


def template_source(name: Optional[str]) -> str:
    if not name:
        return ""
    path = (TEMPLATES_DIR / f"{name}.html").resolve()
    if path.parent != TEMPLATES_DIR.resolve() or not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


def list_templates() -> list[dict[str, Any]]:
    """Tool shape: every starting template and the kind that uses it."""
    out = []
    for kind in KINDS.values():
        if kind.template and template_source(kind.template):
            out.append(
                {
                    "name": kind.template,
                    "kind": kind.name,
                    "about": kind.label,
                    "libraries": list(kind.libraries),
                    "url": f"/stage-templates/{kind.template}.html",
                }
            )
    return out


def get_template_info(name: str) -> Optional[dict[str, Any]]:
    """Tool shape: one template with its source."""
    for item in list_templates():
        if item["name"] == name:
            return {**item, "source": template_source(name)}
    return None


# ---------------------------------------------------------------------------
# the host
# ---------------------------------------------------------------------------


def host_facts() -> str:
    """True, public facts about the streamer and his brand (room/host.md).

    Read on every build, so editing the file needs no restart. Comment lines
    (starting with #) are for the person editing it and are not sent.
    """
    try:
        text = HOST_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""
    lines = [ln.rstrip() for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(ln for ln in lines if ln.strip())[:1500]


# ---------------------------------------------------------------------------
# what the code writer receives
# ---------------------------------------------------------------------------


HOST_MENTION = re.compile(
    r"\b(selwyn|selwynbuilds|the host|the streamer|your (owner|creator|boss|channel))\b",
    re.IGNORECASE,
)


def writer_context(kind: Kind, creating: bool, request_text: str = "") -> dict[str, Any]:
    """Extra request fields for the code writer, for this kind.

    The template is only sent when creating: a change works on the program
    already on screen. Facts about the host are only sent when the request is
    about the host: otherwise every viewer's website came out as "Selwyn
    Builds".
    """
    context: dict[str, Any] = {"kind": kind.name}
    host = host_facts() if HOST_MENTION.search(str(request_text or "")) else ""
    if host:
        context["about_the_host"] = host
    if kind.libraries:
        context["libraries"] = library_lines(kind.libraries)
    if kind.uses_assets:
        context["approved_assets"] = asset_lines()
    if creating and kind.template:
        source = template_source(kind.template)
        if source:
            context["starting_template"] = source
    return context


# ---------------------------------------------------------------------------
# serving
# ---------------------------------------------------------------------------


def install_stage_headers(app: Any) -> None:
    """Let the sandboxed preview load /stage-libs and /stage-assets.

    The preview runs with an opaque origin, so ES modules (Three.js) and
    loaders that fetch files (Phaser) need Access-Control-Allow-Origin on these
    read-only, public folders. Nothing else on the server gets the header.
    """

    @app.middleware("http")
    async def stage_headers(request, call_next):  # pragma: no cover - thin glue
        response = await call_next(request)
        if request.url.path.startswith(("/stage-libs/", "/stage-assets/")):
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
            response.headers["X-Content-Type-Options"] = "nosniff"
        elif request.url.path.startswith(("/vr-agent/", "/stage-templates/")):
            # The Stage page and its scripts change with every update: never
            # let the browser show a stale copy (it once left the preview
            # white). Pinned /stage-libs files can still be cached.
            response.headers["Cache-Control"] = "no-cache"
        return response
