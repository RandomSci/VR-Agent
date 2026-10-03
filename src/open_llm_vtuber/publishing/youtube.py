"""The YouTube side: a live-chat message, a comment reply, a description section.

What the YouTube Data API v3 actually supports (checked against the official
reference, October 2026):

* liveChatMessages.insert posts a plain text message to a live chat (50 quota
  units). There is no reply or thread field and no mention field, so "@name"
  is just text: YouTube may or may not notify that viewer.
* comments.insert with snippet.parentId replies under an existing normal
  video comment (50 units). Live chat and video comments are different
  resources and are kept separate here.
* videos.update replaces the whole snippet (50 units): any snippet field that
  is not sent is deleted, so the current snippet is fetched first and sent
  back with only the description changed. title and categoryId are required.
* One OAuth scope covers all three: https://www.googleapis.com/auth/youtube.force-ssl
* The default daily quota is 10,000 units.

Credentials are an OAuth client (id and secret) plus a refresh token for the
channel, exchanged here for short-lived access tokens. They never leave this
module and are scrubbed from errors.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

import httpx

TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/youtube/v3"
UPLOAD_API = "https://www.googleapis.com/upload/youtube/v3"
SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
# Class mode also uses the same Google account for lesson material:
# Slides (the lesson deck), Docs (notes), Sheets (leaderboard), Calendar
# (the schedule). One sign-in covers all. Google refuses drive.file in the
# same request as YouTube ("scopes that cannot be requested together"), and
# the Slides, Docs and Sheets APIs can create their own files without it.
CLASS_SCOPES = (
    "https://www.googleapis.com/auth/presentations",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/calendar.events",
)
ALL_SCOPES = " ".join((SCOPE, *CLASS_SCOPES))
CHAT_LIMIT = 200  # YouTube live chat messages are short; keep well within it


class YouTubeError(RuntimeError):
    pass


class YouTubeClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        client: Optional[httpx.Client] = None,
        on_refresh_token: Optional[Callable[[str], None]] = None,
    ):
        self._on_refresh_token = on_refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._http = client or httpx.Client(timeout=20)
        self._access_token = ""
        self._expires_at = 0.0

    def _scrub(self, text: str) -> str:
        for secret in (self._client_secret, self._refresh_token, self._access_token):
            if secret:
                text = text.replace(secret, "***")
        return text

    def _token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        try:
            response = self._http.post(
                TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError as exc:
            raise YouTubeError(
                self._scrub(f"Google sign-in unreachable: {exc}")
            ) from None
        if response.status_code >= 400:
            raise YouTubeError(
                self._scrub(
                    f"Google sign-in failed: {response.status_code} {response.text[:200]}"
                )
            )
        data = response.json()
        rotated = str(data.get("refresh_token") or "")
        if rotated and rotated != self._refresh_token:
            self._refresh_token = rotated
            if self._on_refresh_token:
                try:
                    self._on_refresh_token(rotated)
                except Exception:
                    pass  # saving is best effort; the token still works now
        self._access_token = data["access_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 3600))
        return self._access_token

    def _call(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self._token()}"}
        try:
            response = self._http.request(method, API + path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise YouTubeError(self._scrub(f"YouTube unreachable: {exc}")) from None
        if response.status_code >= 400:
            raise YouTubeError(
                self._scrub(
                    f"YouTube {method} {path} failed: {response.status_code} {response.text[:300]}"
                )
            )
        return response.json() if response.content else {}

    # -- finding the stream -------------------------------------------------
    def find_active_broadcast_video_id(self) -> str:
        """Your broadcast that is live right now (1 quota unit). Its id is the video id."""
        data = self._call(
            "GET",
            "/liveBroadcasts",
            params={
                "part": "id,snippet",
                "broadcastStatus": "active",
                "broadcastType": "all",
            },
        )
        items = data.get("items") or []
        return str(items[0].get("id") or "") if items else ""

    def find_live_video_by_channel(self, channel_id: str) -> str:
        """Fallback: public search for a live video on the channel (100 units)."""
        if not channel_id:
            return ""
        data = self._call(
            "GET",
            "/search",
            params={
                "part": "id",
                "channelId": channel_id,
                "eventType": "live",
                "type": "video",
                "maxResults": 1,
            },
        )
        items = data.get("items") or []
        return str((items[0].get("id") or {}).get("videoId") or "") if items else ""

    # -- live chat ----------------------------------------------------------
    def active_live_chat_id(self, video_id: str) -> str:
        """The chat of a broadcast that is live right now, or "" if it ended."""
        data = self._call(
            "GET", "/videos", params={"part": "liveStreamingDetails", "id": video_id}
        )
        items = data.get("items") or []
        if not items:
            return ""
        return str(
            (items[0].get("liveStreamingDetails") or {}).get("activeLiveChatId") or ""
        )

    def post_chat_message(self, live_chat_id: str, text: str) -> dict[str, Any]:
        text = " ".join(str(text or "").split())[:CHAT_LIMIT]
        return self._call(
            "POST",
            "/liveChat/messages",
            params={"part": "snippet"},
            json={
                "snippet": {
                    "liveChatId": live_chat_id,
                    "type": "textMessageEvent",
                    "textMessageDetails": {"messageText": text},
                }
            },
        )

    # -- normal comments ----------------------------------------------------
    def reply_to_comment(self, parent_comment_id: str, text: str) -> dict[str, Any]:
        return self._call(
            "POST",
            "/comments",
            params={"part": "snippet"},
            json={
                "snippet": {
                    "parentId": parent_comment_id,
                    "textOriginal": str(text)[:1000],
                }
            },
        )

    # -- description ----------------------------------------------------------
    def get_snippet(self, video_id: str) -> dict[str, Any]:
        data = self._call("GET", "/videos", params={"part": "snippet", "id": video_id})
        items = data.get("items") or []
        if not items:
            raise YouTubeError("video not found or not yours")
        return items[0]["snippet"]

    def set_description(
        self, video_id: str, snippet: dict[str, Any], description: str
    ) -> dict[str, Any]:
        """Send back the CURRENT snippet with only the description changed."""
        keep = {
            k: snippet[k]
            for k in (
                "title",
                "categoryId",
                "tags",
                "defaultLanguage",
                "defaultAudioLanguage",
            )
            if k in snippet
        }
        keep["description"] = description
        return self._call(
            "PUT",
            "/videos",
            params={"part": "snippet"},
            json={"id": video_id, "snippet": keep},
        )

    # -- the stream itself (title, start time, ending it) ---------------------
    def set_title_and_description(
        self, video_id: str, snippet: dict[str, Any], title: str, description: str
    ) -> dict[str, Any]:
        """Send back the CURRENT snippet with the title and description changed."""
        keep = {
            k: snippet[k]
            for k in ("categoryId", "tags", "defaultLanguage", "defaultAudioLanguage")
            if k in snippet
        }
        keep["title"] = " ".join(str(title).split())[:100]
        keep["description"] = str(description)[:4900]
        return self._call(
            "PUT", "/videos", params={"part": "snippet"}, json={"id": video_id, "snippet": keep}
        )

    def live_started_at(self, video_id: str) -> str:
        """ISO time the broadcast went live, or "" (1 unit)."""
        data = self._call(
            "GET", "/videos", params={"part": "liveStreamingDetails", "id": video_id}
        )
        items = data.get("items") or []
        if not items:
            return ""
        details = items[0].get("liveStreamingDetails") or {}
        if details.get("actualEndTime"):
            return ""
        return str(details.get("actualStartTime") or "")

    def end_broadcast(self, broadcast_id: str) -> dict[str, Any]:
        """Move the broadcast to "complete": the live stream ends (50 units)."""
        return self._call(
            "POST",
            "/liveBroadcasts/transition",
            params={"broadcastStatus": "complete", "id": broadcast_id, "part": "status"},
        )

    # -- playlists ---------------------------------------------------------------
    def find_playlist(self, title: str) -> str:
        """The id of my playlist with exactly this title, or "" (1 unit per page)."""
        token = ""
        for _ in range(5):
            params = {"part": "snippet", "mine": "true", "maxResults": 50}
            if token:
                params["pageToken"] = token
            data = self._call("GET", "/playlists", params=params)
            for item in data.get("items") or []:
                if ((item.get("snippet") or {}).get("title") or "").strip() == title.strip():
                    return str(item.get("id") or "")
            token = data.get("nextPageToken") or ""
            if not token:
                break
        return ""

    def create_playlist(self, title: str, description: str = "") -> str:
        """A new public playlist (50 units)."""
        data = self._call(
            "POST",
            "/playlists",
            params={"part": "snippet,status"},
            json={
                "snippet": {"title": title[:150], "description": description[:4900]},
                "status": {"privacyStatus": "public"},
            },
        )
        return str(data.get("id") or "")

    def playlist_has(self, playlist_id: str, video_id: str) -> bool:
        data = self._call(
            "GET",
            "/playlistItems",
            params={"part": "id", "playlistId": playlist_id, "videoId": video_id, "maxResults": 1},
        )
        return bool(data.get("items"))

    def add_to_playlist(self, playlist_id: str, video_id: str) -> dict[str, Any]:
        """Put a video in a playlist (50 units)."""
        return self._call(
            "POST",
            "/playlistItems",
            params={"part": "snippet"},
            json={"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}},
        )

    def set_thumbnail(self, video_id: str, image: bytes, mime: str = "image/jpeg") -> dict[str, Any]:
        """Upload a custom thumbnail (50 units). The channel must be allowed
        custom thumbnails (phone verified); max 2 MB."""
        headers = {"Authorization": f"Bearer {self._token()}", "Content-Type": mime}
        try:
            response = self._http.post(
                UPLOAD_API + "/thumbnails/set",
                params={"videoId": video_id, "uploadType": "media"},
                headers=headers,
                content=image,
                timeout=60,
            )
        except httpx.HTTPError as exc:
            raise YouTubeError(self._scrub(f"YouTube unreachable: {exc}")) from None
        if response.status_code >= 400:
            raise YouTubeError(self._scrub(f"Thumbnail upload failed: {response.status_code} {response.text[:300]}"))
        return response.json() if response.content else {}

    # -- going live without a click in YouTube Studio ----------------------------
    def stream_for_key(self, key: str) -> dict[str, str]:
        """The liveStream that uses this key: {id, status} or {} (1 unit)."""
        data = self._call(
            "GET", "/liveStreams", params={"part": "id,cdn,status", "mine": "true", "maxResults": 50}
        )
        for item in data.get("items") or []:
            name = ((item.get("cdn") or {}).get("ingestionInfo") or {}).get("streamName") or ""
            if name and name == key:
                status = (item.get("status") or {}).get("streamStatus") or ""
                return {"id": str(item.get("id") or ""), "status": str(status)}
        return {}

    def upcoming_broadcasts(self) -> list[dict[str, Any]]:
        """Broadcasts waiting to start: id, life cycle, bound stream, monitor (1 unit)."""
        data = self._call(
            "GET",
            "/liveBroadcasts",
            params={
                "part": "id,status,contentDetails",
                "broadcastStatus": "upcoming",
                "broadcastType": "all",
                "maxResults": 20,
            },
        )
        out = []
        for item in data.get("items") or []:
            details = item.get("contentDetails") or {}
            out.append(
                {
                    "id": str(item.get("id") or ""),
                    "life": str((item.get("status") or {}).get("lifeCycleStatus") or ""),
                    "stream": str(details.get("boundStreamId") or ""),
                    "monitor": bool((details.get("monitorStream") or {}).get("enableMonitorStream")),
                }
            )
        return out

    def transition(self, broadcast_id: str, status: str) -> dict[str, Any]:
        """testing, live or complete (50 units)."""
        return self._call(
            "POST",
            "/liveBroadcasts/transition",
            params={"broadcastStatus": status, "id": broadcast_id, "part": "status"},
        )

    def create_broadcast(self, title: str, description: str = "") -> str:
        """A new public broadcast that starts by itself when video arrives (50 units)."""
        from datetime import datetime, timezone

        data = self._call(
            "POST",
            "/liveBroadcasts",
            params={"part": "snippet,status,contentDetails"},
            json={
                "snippet": {
                    "title": " ".join(str(title).split())[:100],
                    "description": str(description)[:4900],
                    "scheduledStartTime": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                },
                "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
                "contentDetails": {
                    "enableAutoStart": True,
                    "enableAutoStop": True,
                    "monitorStream": {"enableMonitorStream": False},
                },
            },
        )
        return str(data.get("id") or "")

    def bind(self, broadcast_id: str, stream_id: str) -> dict[str, Any]:
        """Connect a broadcast to the stream key's stream (50 units)."""
        return self._call(
            "POST",
            "/liveBroadcasts/bind",
            params={"id": broadcast_id, "streamId": stream_id, "part": "id,contentDetails"},
        )

    # -- the channel page -------------------------------------------------------
    def channel_branding(self) -> dict[str, Any]:
        data = self._call("GET", "/channels", params={"part": "brandingSettings", "mine": "true"})
        items = data.get("items") or []
        if not items:
            raise YouTubeError("no channel found for this sign-in")
        return items[0]

    def set_channel_description(self, channel: dict[str, Any], description: str) -> dict[str, Any]:
        """Send back the channel's branding with only the description changed."""
        current = dict((channel.get("brandingSettings") or {}).get("channel") or {})
        current.pop("title", None)  # read only
        current["description"] = str(description)[:1000]
        return self._call(
            "PUT",
            "/channels",
            params={"part": "brandingSettings"},
            json={"id": channel["id"], "brandingSettings": {"channel": current}},
        )

    # -- the stream key ---------------------------------------------------------
    def stream_keys(self) -> list[dict[str, str]]:
        """The channel's reusable stream keys: [{title, key}] (1 unit)."""
        data = self._call(
            "GET", "/liveStreams", params={"part": "snippet,cdn", "mine": "true", "maxResults": 20}
        )
        keys = []
        for item in data.get("items") or []:
            key = ((item.get("cdn") or {}).get("ingestionInfo") or {}).get("streamName") or ""
            if key:
                keys.append({"title": str((item.get("snippet") or {}).get("title") or ""), "key": key})
        return keys
