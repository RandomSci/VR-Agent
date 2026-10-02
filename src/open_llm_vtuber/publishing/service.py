"""The trusted publishing service: the only thing Mika's side talks to.

Three narrow actions, each refusing unless its flag is on and the job
qualifies. They never raise into the stream: every failure comes back as
{"ok": False, "reason": ...} and is stored on the job.

    publish_project(job_id)                 GitHub Pages (or the dry-run folder)
    announce_published_project(job_id)      live-chat message, or comment reply
    update_generated_games_section(job_id)  the description section

A job is publishable only when it is a web program whose last real-browser
check passed. Failed or unchecked builds are never uploaded.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Callable, Optional

from loguru import logger

from . import description as desc
from .bundle import BundleError, build_bundle
from .gallery import (
    clean_title,
    entry_for,
    merge_registry,
    render_gallery,
    slug_owner,
)
from .provenance import (
    FAILED,
    PASSED,
    PUBLISH_FAILED,
    PUBLISHED,
    PUBLISHING,
    TESTING,
    CreationJob,
    JobStore,
    safe_slug,
)
from .settings import PublishSettings, save_refresh_token
from .targets import DryRunTarget, GitHubTarget, PublishTargetError
from .youtube import YouTubeClient, YouTubeError


class PublicationService:
    def __init__(
        self,
        settings: PublishSettings,
        store: Optional[JobStore] = None,
        target_factory: Optional[Callable[[], Any]] = None,
        youtube_factory: Optional[Callable[[], YouTubeClient]] = None,
    ):
        self.settings = settings
        self.store = store or JobStore(settings.jobs_file)
        self._target_factory = target_factory or self._default_target
        self._youtube_factory = youtube_factory or self._default_youtube
        self.dry_run_log: list[dict[str, Any]] = []
        self._found_video_id = ""
        self._found_at = 0.0

    # -- wiring ---------------------------------------------------------------
    def _default_target(self):
        if self.settings.dry_run:
            name = self.settings.github_repo.split("/")[-1] or "mika-generated-games"
            return DryRunTarget(self.settings.dry_run_dir / name)
        return GitHubTarget(
            self.settings.github_repo,
            self.settings.github_branch,
            self.settings.github_token,
        )

    def _default_youtube(self) -> YouTubeClient:
        s = self.settings
        token_file = s.youtube_token_file

        def keep(token: str) -> None:
            s.youtube_refresh_token = token
            save_refresh_token(token, token_file)

        return YouTubeClient(
            s.youtube_client_id,
            s.youtube_client_secret,
            s.youtube_refresh_token,
            on_refresh_token=keep,
        )

    VIDEO_ID_TTL = 600.0

    def _video_id(
        self, client: Optional[YouTubeClient] = None, fresh: bool = False
    ) -> str:
        """YOUTUBE_VIDEO_ID if set, else the broadcast that is live right now.

        Found with liveBroadcasts.list (1 unit), cached for ten minutes, and
        searched again when the cached stream has no chat. The channel search
        (100 units) is only a fallback.
        """
        s = self.settings
        if s.youtube_video_id:
            return s.youtube_video_id
        now = time.time()
        if (
            not fresh
            and self._found_video_id
            and now - self._found_at < self.VIDEO_ID_TTL
        ):
            return self._found_video_id
        if not s.youtube_ready:
            return ""
        try:
            client = client or self._youtube_factory()
            found = client.find_active_broadcast_video_id()
            if not found and s.youtube_channel_id:
                found = client.find_live_video_by_channel(s.youtube_channel_id)
        except YouTubeError as exc:
            logger.warning(f"Could not find the live stream: {exc}")
            found = ""
        self._found_video_id, self._found_at = found, (now if found else 0.0)
        if found:
            logger.info(f"Live stream found automatically: {found}")
        return found

    def _base_url(self) -> str:
        if self.settings.pages_base_url:
            return self.settings.pages_base_url
        if self.settings.dry_run:
            return (
                self.settings.dry_run_dir
                / (self.settings.github_repo.split("/")[-1] or "mika-generated-games")
            ).as_uri()
        return ""

    # -- recording what happened on stream ---------------------------------
    def record_creation(
        self,
        *,
        viewer: dict[str, Any],
        request_text: str,
        title: str,
        kind: str,
        language: str,
        built_by: str = "",
    ) -> CreationJob:
        platform = str(viewer.get("platform") or "")
        source = (
            "youtube_live_chat"
            if platform == "youtube"
            else ("youtube_comment" if platform == "youtube_comment" else "dev")
        )
        return self.store.create(
            source=source,
            source_message_id=str(viewer.get("message_id") or "")[:120],
            viewer_display_name=str(viewer.get("display_name") or "")[:80],
            viewer_handle=str(viewer.get("handle") or "")[:80],
            viewer_channel_id=str(viewer.get("author_id") or "")[:80],
            request_text=str(request_text or "")[:500],
            title=clean_title(str(title or request_text or ""))[:80],
            project_type=kind,
            language=language,
            built_by=str(built_by or "").strip().lower()[:20],
            status=TESTING,
        )

    def record_result(
        self, job_id: str, code: str, check: dict[str, Any]
    ) -> Optional[CreationJob]:
        ok = bool(check.get("ok")) and not check.get("skipped")
        job = self.store.get(job_id)
        # A change that broke an already published game must not hide it:
        # the last good version stays live and listed.
        keep_published = job is not None and job.status == PUBLISHED and not ok
        return self.store.update(
            job_id,
            status=PUBLISHED if keep_published else (PASSED if ok else FAILED),
            check=check,
            code_sha256=hashlib.sha256(str(code or "").encode()).hexdigest(),
        )

    # -- 1. publish ---------------------------------------------------------
    def publish_project(
        self, job_id: str, code: str, thumbnail_png: Optional[bytes] = None
    ) -> dict[str, Any]:
        s = self.settings
        job = self.store.get(job_id)
        if job is None:
            return {"ok": False, "reason": "unknown job"}
        if not s.enabled:
            return {
                "ok": False,
                "reason": "publishing is disabled (PUBLISHING_ENABLED=false)",
            }
        if not s.dry_run and not s.github_ready:
            return {"ok": False, "reason": "GitHub publishing is not configured"}
        if job.language != "web":
            return {"ok": False, "reason": "only web programs can be published"}
        if job.status not in (PASSED, PUBLISHED, PUBLISH_FAILED) or not job.check.get(
            "ok"
        ):
            return {
                "ok": False,
                "reason": "only builds that passed the browser check are published",
            }
        if hashlib.sha256(str(code or "").encode()).hexdigest() != job.code_sha256:
            return {
                "ok": False,
                "reason": "the code changed after it was checked; check it again first",
            }

        self.store.update(job_id, status=PUBLISHING, publish_error="")
        try:
            target = self._target_factory()
            registry_bytes = target.read("games.json")
            slug = job.project_slug
            owner = slug_owner(registry_bytes, slug)
            if (owner and owner != job.job_id) or self.store.slug_taken(
                slug, other_than=job.job_id
            ):
                # Never overwrite another viewer's game.
                slug = safe_slug(job.title or job.request_text)
                self.store.update(job_id, project_slug=slug)
            bundle = build_bundle(code, slug)
            base = self._base_url()
            url = f"{base}/games/{slug}/"
            self.store.update(
                job_id, public_url=url, published_at=job.published_at or time.time()
            )
            job = self.store.get(job_id)
            entry = entry_for(job)
            files = {p: d for p, d in bundle.files.items() if not target.has_same(p, d)}
            if thumbnail_png:
                files[f"games/{slug}/thumb.png"] = thumbnail_png
                entry["has_thumbnail"] = True
            registry = merge_registry(registry_bytes, entry)
            files["games.json"] = json.dumps(
                registry, indent=1, ensure_ascii=False
            ).encode("utf-8")
            files["index.html"] = render_gallery(registry, s.live_url).encode("utf-8")
            files.setdefault(".nojekyll", b"")
            target.commit(
                files,
                f"Publish {slug} (requested by {job.viewer_display_name or 'a viewer'})",
            )
        except (BundleError, PublishTargetError, OSError, ValueError) as exc:
            self.store.update(
                job_id, status=PUBLISH_FAILED, publish_error=str(exc)[:300]
            )
            logger.warning(f"Publishing {job_id} failed: {exc}")
            return {"ok": False, "reason": str(exc)[:300]}
        self.store.update(job_id, status=PUBLISHED)
        try:
            self.save_code(job_id, code)  # so it can be reopened later
        except OSError as exc:
            logger.warning(f"Could not keep the code of {job_id}: {exc}")
        logger.info(f"Published {slug}{' (dry run)' if s.dry_run else ''}: {url}")
        # GitHub Pages lets browsers keep a page for 10 minutes. A version in
        # the link makes an update show at once for anyone opening it.
        version = hashlib.sha256(str(code or "").encode()).hexdigest()[:8]
        fresh = url if s.dry_run else f"{url}?v={version}"
        return {
            "ok": True,
            "url": fresh,
            "page": url,
            "slug": slug,
            "dry_run": s.dry_run,
        }

    # -- 2. announce --------------------------------------------------------
    def announcement_text(self, job: CreationJob) -> str:
        who = job.viewer_display_name.strip()
        if who and not who.startswith("@"):
            who = "@" + who
        return (
            f"{who + ' ' if who else ''}your game is up 🎮 {job.public_url} "
            "(give it about 5 minutes to load, or search your name in the gallery)"
        ).strip()

    # -- the code of each published game, kept locally ------------------
    @property
    def code_dir(self):
        return self.settings.jobs_file.parent / "code"

    def save_code(self, job_id: str, code: str) -> None:
        if not job_id.isalnum():
            return
        self.code_dir.mkdir(parents=True, exist_ok=True)
        (self.code_dir / f"{job_id}.html").write_text(code, encoding="utf-8")

    def saved_code(self, job_id: str) -> str:
        """The source of a published game, as it was on the Stage.

        Kept locally since this version; older games are read back from the
        published repository and turned back into Stage paths.
        """
        if not str(job_id).isalnum():
            return ""
        path = self.code_dir / f"{job_id}.html"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            pass
        job = self.store.get(job_id)
        if job is None or not job.project_slug or not job.public_url:
            return ""
        try:
            data = self._target_factory().read(f"games/{job.project_slug}/index.html")
        except Exception as exc:  # the network or GitHub may be unavailable
            logger.warning(f"Could not read {job.project_slug} back: {exc}")
            return ""
        if not data:
            return ""
        page = data.decode("utf-8", errors="replace")
        page = re.sub(
            r'<meta http-equiv="Content-Security-Policy" content="[^"]*">', "", page, count=1
        )
        code = page.replace("../../libs/", "/stage-libs/").replace(
            "../../assets/", "/stage-assets/"
        )
        try:
            self.save_code(job_id, code)
        except OSError:
            pass
        return code

    def youtube_preview(self, job_id: str) -> dict[str, Any]:
        """What WOULD go to YouTube for this job, without sending anything.

        DEV prints this in the terminal while YouTube stays off.
        """
        job = self.store.get(job_id)
        if job is None:
            return {}
        published = self.store.published()
        section = (
            desc.render_section(
                published, self._base_url() + "/", self.settings.description_latest
            )
            if published
            else ""
        )
        return {"chat": self.announcement_text(job), "description": section}

    def announce_published_project(self, job_id: str) -> dict[str, Any]:
        s = self.settings
        job = self.store.get(job_id)
        if job is None or job.status != PUBLISHED:
            return {"ok": False, "reason": "only published jobs are announced"}
        text = self.announcement_text(job)
        if job.source == "youtube_comment":
            if not (s.youtube_enabled and s.youtube_comment_replies_enabled):
                return {"ok": False, "reason": "comment replies are disabled"}
            if job.announced_comment:
                return {"ok": True, "reason": "already replied"}
            return self._youtube_action(
                job,
                "reply_to_comment",
                job.source_message_id,
                text,
                mark={"announced_comment": True},
            )
        if not (s.youtube_enabled and s.youtube_chat_enabled):
            return {"ok": False, "reason": "live chat posting is disabled"}
        if job.announced_chat:
            return {"ok": True, "reason": "already announced"}
        return self._youtube_action(
            job, "post_chat", "", text, mark={"announced_chat": True}
        )

    # -- 3. description -------------------------------------------------------
    def update_generated_games_section(self, job_id: str = "") -> dict[str, Any]:
        s = self.settings
        if not (s.youtube_enabled and s.youtube_description_enabled):
            return {"ok": False, "reason": "description updates are disabled"}
        published = self.store.published()
        if not published:
            return {"ok": False, "reason": "nothing published yet"}
        gallery = self._base_url() + "/"
        if s.dry_run:
            section = desc.render_section(published, gallery, s.description_latest)
            self.dry_run_log.append({"action": "description", "section": section})
            logger.info("Dry run: would update the description section")
            return {"ok": True, "dry_run": True, "section": section}
        if not s.youtube_ready:
            return {"ok": False, "reason": "YouTube credentials are not configured"}
        try:
            client = self._youtube_factory()
            video_id = self._video_id(client)
            if not video_id:
                return {"ok": False, "reason": "no live stream found right now"}
            snippet = client.get_snippet(video_id)
            current = snippet.get("description", "")
            updated = desc.fit_description(
                current, published, gallery, s.description_latest
            )
            if updated == current:
                return {"ok": True, "reason": "already up to date"}
            client.set_description(video_id, snippet, updated)
        except YouTubeError as exc:
            logger.warning(f"Description update failed: {exc}")
            return {"ok": False, "reason": str(exc)[:300]}
        for job in published:
            if not job.in_description:
                self.store.update(job.job_id, in_description=True)
        return {"ok": True}

    # -- helpers --------------------------------------------------------------
    def _youtube_action(
        self,
        job: CreationJob,
        action: str,
        target_id: str,
        text: str,
        mark: dict[str, Any],
    ) -> dict[str, Any]:
        if self.settings.dry_run:
            if action == "post_chat":
                target_id = (
                    self.settings.youtube_video_id
                    or "(live stream, found automatically)"
                )
            self.dry_run_log.append(
                {"action": action, "target": target_id, "text": text}
            )
            logger.info(f"Dry run: would {action.replace('_', ' ')}: {text}")
            return {"ok": True, "dry_run": True, "text": text}
        if not self.settings.youtube_ready:
            return {"ok": False, "reason": "YouTube credentials are not configured"}
        try:
            client = self._youtube_factory()
            if action == "post_chat":
                video_id = self._video_id(client)
                chat_id = client.active_live_chat_id(video_id) if video_id else ""
                if not chat_id and not self.settings.youtube_video_id:
                    # The cached stream ended; look once more for a new one.
                    video_id = self._video_id(client, fresh=True)
                    chat_id = client.active_live_chat_id(video_id) if video_id else ""
                if not chat_id:
                    return {
                        "ok": False,
                        "reason": "the stream is not live, so there is no chat",
                    }
                client.post_chat_message(chat_id, text)
            else:
                client.reply_to_comment(target_id, text)
        except YouTubeError as exc:
            logger.warning(f"YouTube {action} failed: {exc}")
            return {"ok": False, "reason": str(exc)[:300]}
        self.store.update(job.job_id, **mark)
        return {"ok": True, "text": text}
