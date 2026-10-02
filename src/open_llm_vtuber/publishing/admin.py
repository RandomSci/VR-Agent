"""The gallery admin: list published games and take one down.

Served only by the local server (http://127.0.0.1:12393/vr-agent/admin.html).
The public gallery on GitHub Pages is static, so a password there would be
readable by anyone; deleting needs the GitHub token, which never leaves this
machine. The password is PASS_W in .env.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import time
from collections import deque

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from .service import PublicationService
from .settings import PublishSettings

_failures: deque[float] = deque(maxlen=20)


def admin_password() -> str:
    return (os.environ.get("PASS_W") or os.environ.get("pass_w") or "").strip()


def _check(payload: dict) -> JSONResponse | None:
    expected = admin_password()
    if not expected:
        return JSONResponse(
            {"ok": False, "reason": "Set PASS_W in .env and restart the server."},
            status_code=403,
        )
    now = time.time()
    if sum(1 for t in _failures if now - t < 60) >= 5:
        return JSONResponse(
            {"ok": False, "reason": "Too many wrong passwords. Wait a minute."},
            status_code=429,
        )
    given = str(payload.get("password") or "")
    if not hmac.compare_digest(given.encode(), expected.encode()):
        _failures.append(now)
        return JSONResponse({"ok": False, "reason": "Wrong password."}, status_code=401)
    return None


async def _payload(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {}


def _service() -> PublicationService:
    return PublicationService(PublishSettings.from_env())


def init_admin_routes() -> APIRouter:
    router = APIRouter()

    @router.post("/vr-agent/admin/games")
    async def admin_games(request: Request):
        payload = await _payload(request)
        denied = _check(payload)
        if denied:
            return denied
        service = _service()
        if not service.settings.enabled:
            return JSONResponse({"ok": False, "reason": "Publishing is off in .env."})
        try:
            games = await asyncio.to_thread(service.list_published)
        except Exception as exc:  # GitHub unreachable and the like
            return JSONResponse({"ok": False, "reason": str(exc)[:300]})
        return JSONResponse(
            {
                "ok": True,
                "gallery": service._base_url() + "/",
                "games": [
                    {
                        "slug": g.get("slug"),
                        "title": g.get("title"),
                        "requested_by": g.get("requested_by"),
                        "built_by": g.get("built_by"),
                        "published_at": g.get("published_at"),
                    }
                    for g in games
                ],
            }
        )

    @router.post("/vr-agent/admin/delete")
    async def admin_delete(request: Request):
        payload = await _payload(request)
        denied = _check(payload)
        if denied:
            return denied
        service = _service()
        result = await asyncio.to_thread(service.unpublish, str(payload.get("slug") or ""))
        return JSONResponse(result)

    return router
