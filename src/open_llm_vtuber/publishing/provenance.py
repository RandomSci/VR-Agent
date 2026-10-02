"""Who asked for what: one record per creation, in a small JSON file.

A creation starts when a viewer asks for a web program and keeps its job id
through later changes to that same program. A different program (a fresh
build) is a new creation.

Viewer text never becomes a path: the slug is made only from lowercase ASCII
letters, digits and hyphens, is length-limited and gets a random suffix, and
is checked again wherever it is used.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

# Every state a creation can be in.
REQUESTED = "requested"
GENERATING = "generating"
TESTING = "testing"
PASSED = "passed"
FAILED = "failed"
PUBLISHING = "publishing"
PUBLISHED = "published"
PUBLISH_FAILED = "publish_failed"
STATUSES = (
    REQUESTED,
    GENERATING,
    TESTING,
    PASSED,
    FAILED,
    PUBLISHING,
    PUBLISHED,
    PUBLISH_FAILED,
)

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")
_STOP = {
    "a",
    "an",
    "the",
    "make",
    "me",
    "please",
    "can",
    "you",
    "of",
    "with",
    "where",
    "is",
    "and",
    "for",
    "to",
}


def safe_slug(text: str, suffix: Optional[str] = None) -> str:
    """'MAKE MY GAME!!!! ../../../x' -> 'my-game-x-a82f'. Never a path."""
    ascii_text = (
        unicodedata.normalize("NFKD", str(text or ""))
        .encode("ascii", "ignore")
        .decode()
    )
    words = [w for w in re.split(r"[^a-z0-9]+", ascii_text.lower()) if w]
    kept = [w for w in words if w not in _STOP] or words or ["creation"]
    base = "-".join(kept)[:36].strip("-") or "creation"
    suffix = suffix or secrets.token_hex(2)
    slug = f"{base}-{suffix}"
    if not is_safe_slug(slug):
        raise ValueError(f"unsafe slug {slug!r}")
    return slug


def is_safe_slug(slug: str) -> bool:
    return bool(SLUG_RE.match(str(slug or ""))) and ".." not in slug


@dataclass
class CreationJob:
    job_id: str
    source: str  # youtube_live_chat | youtube_comment | dev
    source_message_id: str = ""
    viewer_display_name: str = ""
    viewer_handle: str = ""
    viewer_channel_id: str = ""  # stable YouTube id; the display name can change
    request_text: str = ""
    title: str = ""
    project_slug: str = ""
    project_type: str = ""  # capabilities kind, e.g. web_game_flyer
    language: str = ""
    status: str = REQUESTED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    check: dict[str, Any] = field(default_factory=dict)
    public_url: str = ""
    published_at: float = 0.0
    publish_error: str = ""
    announced_chat: bool = False
    announced_comment: bool = False
    in_description: bool = False
    code_sha256: str = ""
    built_by: str = ""  # mika or luna: who coded it on stream

    def snapshot(self) -> dict[str, Any]:
        return asdict(self)


class JobStore:
    """A JSON registry of creations. Writes are atomic (temp file + rename)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._jobs: dict[str, CreationJob] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for raw in data.get("jobs", []):
            known = {
                k: v for k, v in raw.items() if k in CreationJob.__dataclass_fields__
            }
            job = CreationJob(**known)
            self._jobs[job.job_id] = job

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"jobs": [j.snapshot() for j in self._jobs.values()]},
            indent=1,
            ensure_ascii=False,
        )
        fd, tmp = tempfile.mkstemp(
            dir=self.path.parent, prefix=".jobs-", suffix=".json"
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp, self.path)

    def create(self, **fields_: Any) -> CreationJob:
        with self._lock:
            job_id = secrets.token_hex(6)
            title = str(fields_.pop("title", "") or fields_.get("request_text", ""))[
                :80
            ]
            job = CreationJob(job_id=job_id, title=title, **fields_)
            if not job.project_slug:
                job.project_slug = safe_slug(title or job.request_text)
            self._jobs[job_id] = job
            self._save()
            return job

    def get(self, job_id: str) -> Optional[CreationJob]:
        return self._jobs.get(str(job_id or ""))

    def update(self, job_id: str, **changes: Any) -> Optional[CreationJob]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for key, value in changes.items():
                if key in CreationJob.__dataclass_fields__ and key != "job_id":
                    setattr(job, key, value)
            if job.status not in STATUSES:
                raise ValueError(f"unknown status {job.status}")
            job.updated_at = time.time()
            self._save()
            return job

    def all(self) -> list[CreationJob]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at)

    def published(self) -> list[CreationJob]:
        return [j for j in self.all() if j.status == PUBLISHED and j.public_url]

    def slug_taken(self, slug: str, other_than: str = "") -> bool:
        return any(
            j.project_slug == slug and j.job_id != other_than
            for j in self._jobs.values()
        )
