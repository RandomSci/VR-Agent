"""Publishing configuration: feature flags and credentials, from the environment.

Every external action is OFF unless its flag is true. Secrets are read here
and nowhere else, and are never printed (repr and describe() mask them).

A .env file next to the project is read if present (without overriding
variables already set in the real environment). .env must stay gitignored.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[3]

SECRET_FIELDS = {"github_token", "youtube_client_secret", "youtube_refresh_token"}

# Written once by scripts/youtube_authorize.py, then kept up to date by the
# server. Gitignored, readable only by you (600, folder 700).
TOKEN_FILE = ROOT / "data" / "secrets" / "youtube_token.json"


def read_saved_refresh_token(path: Optional[Path] = None) -> str:
    path = path or TOKEN_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("refresh_token") or "").strip()


def save_refresh_token(token: str, path: Optional[Path] = None) -> Path:
    """Atomic write, owner-only permissions. Never logs the token."""
    path = path or TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".token-")
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"refresh_token": token.strip()}, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def _flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _text(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def load_dotenv(path: Optional[Path] = None) -> None:
    """Minimal .env reader: KEY=value lines, # comments, no overriding."""
    path = path or ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class PublishSettings:
    # Master switch: nothing leaves this machine while false.
    enabled: bool = False
    # Write what would be published into a local folder instead of GitHub
    # and only log YouTube actions. Safe to test with no credentials.
    dry_run: bool = True
    dry_run_dir: Path = field(default_factory=lambda: ROOT / "data" / "publish-dry-run")
    # Publish automatically when a web build passes its checks. Off: only on
    # request (/publish in the DEV chat window).
    auto_publish: bool = False

    github_repo: str = ""  # owner/name of the generated-games repository
    github_branch: str = "main"
    github_token: str = ""  # fine-grained token: this one repo, Contents write
    pages_base_url: str = ""  # https://<user>.github.io/<repo>
    # Where people can watch live (the gallery's "Watch live" button).
    live_url: str = "https://www.youtube.com/@SelwynBuilds-j1s"

    youtube_enabled: bool = False
    youtube_chat_enabled: bool = False
    youtube_description_enabled: bool = False
    youtube_comment_replies_enabled: bool = False
    # Empty is normal: the live broadcast is found automatically.
    youtube_video_id: str = ""
    youtube_channel_id: str = ""  # only for the fallback live search
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_refresh_token: str = ""
    description_latest: int = 5
    youtube_token_file: Path = field(default_factory=lambda: TOKEN_FILE)

    jobs_file: Path = field(
        default_factory=lambda: ROOT / "data" / "creations" / "jobs.json"
    )

    @classmethod
    def from_env(cls, read_dotenv: bool = True) -> "PublishSettings":
        if read_dotenv:
            load_dotenv()
        return cls(
            enabled=_flag("PUBLISHING_ENABLED"),
            dry_run=_flag("PUBLISHING_DRY_RUN", True),
            auto_publish=_flag("PUBLISHING_AUTO"),
            github_repo=_text("GITHUB_PUBLISH_REPO"),
            github_branch=_text("GITHUB_PUBLISH_BRANCH", "main"),
            github_token=_text("GITHUB_PUBLISH_TOKEN"),
            pages_base_url=_text("GITHUB_PAGES_BASE_URL").rstrip("/"),
            live_url=_text(
                "LIVE_STREAM_URL", "https://www.youtube.com/@SelwynBuilds-j1s"
            ),
            youtube_enabled=_flag("YOUTUBE_PUBLISHING_ENABLED"),
            youtube_chat_enabled=_flag("YOUTUBE_AUTO_CHAT_REPLY"),
            youtube_description_enabled=_flag("YOUTUBE_AUTO_DESCRIPTION"),
            youtube_comment_replies_enabled=_flag("YOUTUBE_AUTO_COMMENT_REPLY"),
            youtube_video_id=_text("YOUTUBE_VIDEO_ID"),
            youtube_channel_id=_text("YOUTUBE_CHANNEL_ID"),
            youtube_client_id=_text("YOUTUBE_CLIENT_ID"),
            youtube_client_secret=_text("YOUTUBE_CLIENT_SECRET"),
            # .env wins if filled; otherwise the token saved by the sign-in.
            youtube_refresh_token=_text("YOUTUBE_REFRESH_TOKEN")
            or read_saved_refresh_token(TOKEN_FILE),
            description_latest=max(
                1, min(10, int(_text("YOUTUBE_DESCRIPTION_LATEST", "5") or 5))
            ),
        )

    # -- what may happen ---------------------------------------------------
    @property
    def github_ready(self) -> bool:
        return bool(self.github_repo and self.github_token and self.pages_base_url)

    @property
    def youtube_ready(self) -> bool:
        return bool(
            self.youtube_client_id
            and self.youtube_client_secret
            and self.youtube_refresh_token
        )

    def describe(self) -> dict[str, object]:
        """Safe to log: secrets appear only as set or missing."""
        out: dict[str, object] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in SECRET_FIELDS:
                out[f.name] = "set" if value else "missing"
            else:
                out[f.name] = str(value) if isinstance(value, Path) else value
        return out

    def __repr__(self) -> str:  # never let a secret reach a log line
        return f"PublishSettings({self.describe()})"
