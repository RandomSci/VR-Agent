# How Code in Public works

Mika and Luna build programs live when viewers ask. GPT-4o-mini stays the
brain. What changed is what it gets to work with. It now has a fixed menu of program
kinds, pinned libraries, working templates, approved art, a real-browser
check and one repair pass.

## The path of one request

1. A viewer comment reaches the director (`room/director.py`).
2. One cheap decision call (`room/coding_actions.py`, `DECIDE_SYSTEM`) returns
   the action, the **kind**, whether it is a **fresh** program, and a **brief**
   that resolves "yeah update it" from recent chat. No keyword or regex
   matching of viewer text anywhere.
3. For a build, Mika says a short line right away (`room/acknowledge.py`, no
   LLM call) while the Stage shows "Mika is writing the code".
4. The code writer (`runtime.generate_code`) gets the live-stream rules, the
   guide for that kind, the pinned library URL, the approved asset list and,
   for a new program, the starting template (`room/capabilities.py`).
5. Web programs are opened in headless Chromium exactly like the Stage shows
   them (`room/browser_qa.py`). Errors, failed files, blocked requests, a
   blank screen and no motion are caught. Python programs run in the
   bubblewrap sandbox as before.
6. If either fails, ONE repair pass gets the real problems. A repair that
   makes things worse is thrown away. Never a loop.
7. Mika explains only what really happened (`director._verification_note`).
8. The creation is recorded with who asked (`publishing/provenance.py`).
   Publishing happens only when you ask, or when auto-publish is on.

## Kinds

| Kind | Language | Built with | Template |
| --- | --- | --- | --- |
| python_lesson | Python | standard library, prints | none |
| python_chart | Python | matplotlib | none |
| python_stats | Python | seaborn, pandas | none |
| python_science | Python | numpy, scipy, SciencePlots (no-latex) | none |
| python_math | Python | sympy, matplotlib | none |
| python_data | Python | pandas | none |
| python_animation | Python | matplotlib FuncAnimation to output.gif | none |
| python_image | Python | Pillow | none |
| web_game_flyer | Web | Phaser 3.90.0 | game-flyer |
| web_game_shooter | Web | Phaser 3.90.0 | game-shooter |
| web_game | Web | Phaser 3.90.0 | game-arena |
| web_creative | Web | p5.js 1.11.13 | creative-p5 |
| web_3d | Web | Three.js 0.186.1 (ES module) | scene-3d |
| web_physics | Web | Matter.js 0.20.0 | physics-matter |
| web_chart | Web | Chart.js 4.5.1 | dashboard-chart |
| web_site | Web | plain HTML, CSS and JS, scrolls itself | site-scroll |
| web_page | Web | plain HTML and CSS, one screen | landing-page |
| web_canvas | Web | plain canvas | skeleton |

p5 1.x and Phaser 3 are pinned on purpose. GPT-4o-mini knows their APIs well,
and the 2.x and 4.x lines changed them.

## Files

```
frontend/stage-libs/      pinned libraries + licenses + manifest.json
frontend/stage-assets/    original art + Mika and Luna sprites + catalog.json
frontend/stage-templates/ nine starting templates (each plays itself)
frontend/vr-agent/stage-preview.js   the one locked-down preview (Stage and checker)
frontend/vr-agent/stage-qa.html      the page the browser checker runs programs on
```

Libraries are stored locally, not loaded from a CDN. A CDN problem in the
middle of a live build would show a black preview, local files cannot be
changed from outside, they are what lets the preview block the internet
entirely, and published games keep working without a third party.

## Websites

A website kind (`web_site`) is a real multi-section site: sticky menu, hero,
services, work, process, contact. Nobody can scroll on a stream, so the page
tours itself: it scrolls section by section, elements fade in as they arrive,
a soft cursor glides to buttons and cards and hovers them, then it loops back
to the top. It is responsive, so a published site also works on a phone.

One-screen pages (`web_page`) must fit the screen. The browser check now
measures the page, and content that is cut off counts as a problem, which
triggers the one repair pass.

## What Mika and Luna know about you

`room/host.md` holds true, public facts about you and Selwyn Builds. It goes
to the code writer with every build, so "make a website for Selwyn Builds"
uses real details. Edit it any time; no restart needed. Lines starting with
`#` are notes and are not sent. Nothing about a real person or business is
invented; missing details become clearly generic copy.

## Faces and reactions while coding

No extra AI calls, only the characters' own Live2D faces and pre-written lines
(`room/coding_mood.py`):

| Moment | Face | Line |
| --- | --- | --- |
| writing or running takes over 2 s | focused (closed eyes, head tilt) | none |
| still going after about 15 s | tired | one grumble, "This one's a big one..." |
| the check or run finds a bug | pout | "AHHH, a bug again?! Hold on, I'm squashing it!" |
| the repair works | sparkly eyes | "Bug defeated!" |
| still broken after the repair | sad | none (she explains honestly) |

Lines never repeat back to back and never talk over another line. Edit the
lists in `coding_mood.py` to change the personality.

When a JavaScript error happens, the check now reports the exact line and
its code (for example `Unexpected token 'var' (line 57: let b = 2 var c)`),
so the repair can go straight to it.

## Going back to earlier games

Every published game keeps its source (`data/creations/code/`; games from
before this was added are read back from the games site). Mika and Luna see a
short list of recent games, so "go back to the flappy bird game and make it
faster" reopens that game on the Stage, changes it and updates its original
page and link.

* You (the host) and the viewer who asked for a game change the original.
* Anyone else gets a remix: a new game that credits the original. Nobody can
  wreck someone else's game.
* A request that could mean either "change it" or "make a new one" is a change
  to what is on screen. New, another or a clearly different kind of thing
  starts a new game.

## Roast and build

Mika and Luna are streamers with opinions, not assistants. When a request is
very simple, silly or absurd they tease the idea (never the person) while
happily building it. When something breaks they get openly, playfully upset
in their own style (Mika dramatic, Luna deadpan), then say honestly what is
wrong. Bug and fix moments also play body motions (Mika's spell fizzling,
Luna's side eye) on top of the faces, and long builds get small fidgets.

## The gallery

`https://<user>.github.io/<repo>/` lists every game with a search box (your
name, a game, Mika or Luna), a Share with friends button on every game, and a
Watch live button (`LIVE_STREAM_URL`, default your YouTube channel). A link
like `.../?q=ana` opens the gallery already filtered.

## Optional: a stronger model for code only

`VR_CODE_MODEL=gpt-5-mini` in `.env` makes code writing and repairs use that
model while chat stays on GPT-4o-mini. If it fails, the chat model writes the
code instead, so a build never fails because of it. Off by default.

## Safety on a public stream

* Web previews run in `<iframe sandbox="allow-scripts">`, so no popups, no
  navigation, no downloads, no access to the Stage. A Content-Security-Policy
  allows scripts, images and data only from this server's `/stage-libs/` and
  `/stage-assets/`. Chat cannot make the stream load anything from the
  internet.
* Python runs in bubblewrap with no network, process and memory limits and a
  time limit, and now with an **empty environment**. Before this change the
  server's environment variables (API keys, tokens) were passed into
  generated code.
* Generated code never sees publishing credentials. They live only in the
  `publishing` package, are never logged and are scrubbed from errors.
* Viewer text never becomes a path. Slugs are lowercase letters, digits and
  hyphens with a random suffix, and every published file is checked to be a
  real, non-symlink file inside the approved folders.

## Publishing (off by default)

```
publishing/settings.py     flags and credentials from .env
publishing/provenance.py   who asked, statuses, safe slugs (data/creations/jobs.json)
publishing/bundle.py       Stage program -> static folder for GitHub Pages
publishing/gallery.py      games.json and the gallery page
publishing/targets.py      GitHub (one commit, fast-forward only) or dry-run folder
publishing/youtube.py      live chat message, comment reply, description
publishing/description.py  the generated description section (idempotent)
publishing/service.py      publish_project, announce_published_project,
                           update_generated_games_section
```

Only a web program whose latest browser check passed, with exactly the code
that was checked, can be published. Python output is not published.

One repository holds every game, with `games/<slug>/index.html`, shared `libs/`
and `assets/`, `games.json` and a gallery `index.html`.

### What YouTube supports (checked against the API reference, Oct 2026)

* Live chat. `liveChatMessages.insert` posts plain text, 50 quota units.
  There is no reply-to-message or thread field and no mention field. "@name"
  in the text is just text, so YouTube may or may not notify that viewer.
* Normal comments. `comments.insert` with the original comment id as
  `parentId` posts a real reply, 50 units. Live chat and video comments are
  separate resources and separate source types here.
* Description. `videos.update` replaces the whole snippet, 50 units. The
  current title, category and tags are fetched and sent back unchanged.
  Descriptions are limited to 5000 bytes and cannot contain `<` or `>`.
* One OAuth scope covers all of it, `https://www.googleapis.com/auth/youtube.force-ssl`.
* Default quota is 10,000 units a day, enough for about 95 published games with a chat
  post and a description update each.

## Environment setup

Copy `.env.example` to `.env`. Every external action is off until you turn it on.

```
PUBLISHING_ENABLED=false
PUBLISHING_DRY_RUN=true
PUBLISHING_AUTO=false
GITHUB_PUBLISH_REPO=YourGitHubName/mika-generated-games
GITHUB_PUBLISH_BRANCH=main
GITHUB_PUBLISH_TOKEN=
GITHUB_PAGES_BASE_URL=https://YourGitHubName.github.io/mika-generated-games
YOUTUBE_PUBLISHING_ENABLED=false
YOUTUBE_AUTO_CHAT_REPLY=false
YOUTUBE_AUTO_DESCRIPTION=false
YOUTUBE_AUTO_COMMENT_REPLY=false
YOUTUBE_VIDEO_ID=
YOUTUBE_CHANNEL_ID=
YOUTUBE_CLIENT_ID=
YOUTUBE_CLIENT_SECRET=
YOUTUBE_REFRESH_TOKEN=
YOUTUBE_DESCRIPTION_LATEST=5
```

| Variable | Secret | Required | What it does and where it comes from |
| --- | --- | --- | --- |
| PUBLISHING_ENABLED | no | yes | Master switch for publishing. |
| PUBLISHING_DRY_RUN | no | yes | true writes to `data/publish-dry-run/` and only logs YouTube actions. |
| PUBLISHING_AUTO | no | no | true publishes every passing web build; false only on `/publish`. |
| GITHUB_PUBLISH_REPO | no | for GitHub | `owner/name` of the games repo you create. Not VR-Agent. |
| GITHUB_PUBLISH_BRANCH | no | no | Branch GitHub Pages serves, default `main`. |
| GITHUB_PUBLISH_TOKEN | YES | for GitHub | Fine-grained token from github.com, Settings, Developer settings, Fine-grained tokens. Repository access is only the games repo. Permission is Contents, Read and write. Nothing else. Set an expiry. |
| GITHUB_PAGES_BASE_URL | no | for GitHub | `https://<user>.github.io/<repo>` once Pages is on. |
| YOUTUBE_PUBLISHING_ENABLED | no | for YouTube | Master switch for every YouTube action. |
| YOUTUBE_AUTO_CHAT_REPLY | no | no | Post "@viewer your game is live" in live chat. |
| YOUTUBE_AUTO_DESCRIPTION | no | no | Keep the generated games section in the description. |
| YOUTUBE_AUTO_COMMENT_REPLY | no | no | Reply under normal video comments that asked for a game. |
| YOUTUBE_VIDEO_ID | no | no | Leave empty. The broadcast that is live right now on your channel is found automatically (liveBroadcasts.list, 1 quota unit), cached for ten minutes and found again when a stream ends. Fill only to force one video. |
| YOUTUBE_CHANNEL_ID | no | no | Your channel id (UC...). Used only as a fallback live search (100 units) if the automatic lookup finds nothing. |
| YOUTUBE_CLIENT_ID | no | for YouTube | In Google Cloud console, enable YouTube Data API v3, set up the OAuth consent screen, then Credentials, OAuth client ID of type Desktop app. |
| YOUTUBE_CLIENT_SECRET | YES | for YouTube | Same OAuth client. |
| YOUTUBE_REFRESH_TOKEN | YES | no | Leave empty. Run `uv run python scripts/youtube_authorize.py` once and sign in with the streaming channel (scope youtube.force-ssl only). The token is saved to `data/secrets/youtube_token.json` (gitignored, mode 600) and the server keeps it up to date. Set the OAuth consent screen to "In production" or Google expires it after 7 days. Fill only to override the file. |
| YOUTUBE_DESCRIPTION_LATEST | no | no | How many newest games the description lists, 1 to 10. |

Why a fine-grained token and not a GitHub App? A fine-grained token can be
limited to the one games repository and to Contents only, it expires, and it
needs no extra service. A GitHub App gives one-hour tokens and is the upgrade
path if this grows (only `publishing/targets.py` would change).

## Testing safely, one step at a time

1. **Dry run.** `PUBLISHING_ENABLED=true` (dry run stays true). Build a web
   game on the DEV Stage, type `/publish` in the chat window. Open
   `data/publish-dry-run/mika-generated-games/index.html` and the game.
2. **GitHub.** Create a public repo `mika-generated-games` with a README.
   In Settings then Pages, deploy from branch `main`, folder root. Create the
   token, fill the GitHub variables, set `PUBLISHING_DRY_RUN=false`, `/publish`.
   The URL works a minute later, after Pages builds.
3. **Live chat.** Run `uv run python scripts/youtube_authorize.py` once.
   Start an unlisted test stream, set `YOUTUBE_PUBLISHING_ENABLED=true` and
   `YOUTUBE_AUTO_CHAT_REPLY=true`, `/publish` again. The live stream is found
   on its own and the message appears in its chat.
4. **Description.** On the same unlisted video, `YOUTUBE_AUTO_DESCRIPTION=true`
   and `/publish`. Your text stays; only the section between the two marker
   lines changes. Run it twice and nothing is duplicated.

`/jobs` in the chat window lists every creation, who asked and its status.

### DEV: real GitHub, YouTube printed in the terminal

With `PUBLISHING_ENABLED=true`, `PUBLISHING_DRY_RUN=false`, `PUBLISHING_AUTO=true`
and `YOUTUBE_PUBLISHING_ENABLED=false`, every web build that passes the browser
check is published to your games site. A change to the same program updates
the same page (same link), and a change that breaks the game leaves the last
good version live. The DEV terminal prints the link, the live chat message
and the description section that YouTube would get, so nothing reaches
YouTube while you test. Response times are hidden; type `/timing` to show
them.

## Turning things off immediately

Set the flag to false in `.env` and restart `./start-teaching.sh`:

| Stop | Flag |
| --- | --- |
| all publishing | PUBLISHING_ENABLED=false |
| real uploads only | PUBLISHING_DRY_RUN=true |
| automatic publishing | PUBLISHING_AUTO=false |
| every YouTube action | YOUTUBE_PUBLISHING_ENABLED=false |
| chat posts | YOUTUBE_AUTO_CHAT_REPLY=false |
| description updates | YOUTUBE_AUTO_DESCRIPTION=false |
| comment replies | YOUTUBE_AUTO_COMMENT_REPLY=false |
| repairs | VR_CODE_REPAIRS=0 |
| the browser check | start the server with `--no-browser-check` |

To revoke access entirely, delete the token on GitHub, remove the app at
myaccount.google.com/permissions and delete `data/secrets/youtube_token.json`.
