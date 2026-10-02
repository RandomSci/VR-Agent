"""The generated "games built live by Mika and Luna" section of a video description.

Only the text between the two marker lines is ever replaced; everything you
wrote above or below it is kept exactly. Running it again with the same games
gives the same description (idempotent), so a game can never be listed twice.

YouTube descriptions are limited to 5000 bytes and may not contain < or >.
"""

from __future__ import annotations

from .provenance import CreationJob

START = "🎮 GAMES BUILT LIVE BY MIKA AND LUNA"
END = "🎮 END OF MIKA AND LUNA'S GAMES"
MAX_BYTES = 5000


def _clean(text: str, limit: int) -> str:
    text = " ".join(str(text or "").replace("<", "").replace(">", "").split())
    return text[:limit]


def render_section(jobs: list[CreationJob], gallery_url: str, latest: int = 5) -> str:
    newest = sorted(jobs, key=lambda j: j.published_at, reverse=True)[:latest]
    lines = [START, "", "Latest creations:", ""]
    for job in newest:
        lines.append(f"• {_clean(job.title or job.project_slug, 60)}")
        who = _clean(job.viewer_display_name or "a viewer", 40)
        builder = _clean(job.built_by.title(), 20) if job.built_by else ""
        lines.append(
            f"  Built by {builder}, requested by {who}" if builder else f"  Requested by {who}"
        )
        lines.append(f"  {job.public_url}")
        lines.append("")
    if gallery_url:
        lines += [
            "Asked for a game on stream? Open the gallery and search your username.",
            "New games can take about 5 minutes to show up. Enjoy!",
            gallery_url,
            "",
        ]
    lines.append(END)
    return "\n".join(lines)


def update_description(description: str, section: str) -> str:
    """Replace the generated section (or add it at the end). Idempotent."""
    text = str(description or "")
    start = text.find(START)
    end = text.find(END, start + 1) if start >= 0 else -1
    if start >= 0 and end >= 0:
        updated = text[:start] + section + text[end + len(END) :]
    else:
        updated = (text.rstrip() + "\n\n" + section) if text.strip() else section
    return updated


def fit_description(
    description: str, jobs: list[CreationJob], gallery_url: str, latest: int
) -> str:
    """The updated description, dropping the oldest games until it fits."""
    for count in range(latest, -1, -1):
        updated = update_description(
            description, render_section(jobs, gallery_url, count)
        )
        if len(updated.encode("utf-8")) <= MAX_BYTES:
            return updated
    return description  # your own text alone is already at the limit: change nothing
