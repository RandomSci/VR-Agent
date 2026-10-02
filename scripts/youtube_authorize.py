"""Sign in to YouTube for publishing, once, on your own computer.

    uv run python scripts/youtube_authorize.py

Needs YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env (an OAuth client of
type "Desktop app" from Google Cloud, with the YouTube Data API v3 enabled).
It opens Google's consent page in your browser, asks only for the
youtube.force-ssl scope (post to live chat, reply to comments, edit video
descriptions), and saves the refresh token to data/secrets/youtube_token.json
(gitignored, readable only by you). Leave YOUTUBE_REFRESH_TOKEN empty in .env:
the server reads the saved token and keeps it up to date. Sign in with the
channel that streams. The token is never printed.

If the OAuth consent screen is still in "Testing", Google expires the token
after 7 days. Set it to "In production" once and it keeps working.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from open_llm_vtuber.publishing.settings import (  # noqa: E402
    TOKEN_FILE,
    PublishSettings,
    save_refresh_token,
)
from open_llm_vtuber.publishing.youtube import SCOPE, TOKEN_URL  # noqa: E402

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"


def main() -> None:
    settings = PublishSettings.from_env()
    if not (settings.youtube_client_id and settings.youtube_client_secret):
        sys.exit("Put YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET in .env first.")

    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    state = secrets.token_urlsafe(16)
    received: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(self.path).query))
            received.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h1>Done. You can close this tab.</h1>".encode())

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    redirect = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    url = (
        AUTH_URL
        + "?"
        + urllib.parse.urlencode(
            {
                "client_id": settings.youtube_client_id,
                "redirect_uri": redirect,
                "response_type": "code",
                "scope": SCOPE,
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    print("Opening Google sign-in. If no browser opens, visit:\n" + url)
    webbrowser.open(url)
    thread.join(timeout=300)
    server.server_close()

    if received.get("state") != state or "code" not in received:
        sys.exit(f"Sign-in did not complete: {received.get('error', 'no answer')}")
    response = httpx.post(
        TOKEN_URL,
        data={
            "client_id": settings.youtube_client_id,
            "client_secret": settings.youtube_client_secret,
            "code": received["code"],
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect,
        },
        timeout=20,
    )
    if response.status_code >= 400:
        sys.exit(f"Google refused the code: {response.status_code}")
    token = response.json().get("refresh_token")
    if not token:
        sys.exit(
            "Google returned no refresh token. Remove the app's access at "
            "myaccount.google.com/permissions and run this again."
        )
    path = save_refresh_token(token, TOKEN_FILE)
    print(
        f"\nSigned in. Token saved to {path.relative_to(ROOT)} (only you can read it)."
    )
    print("Leave YOUTUBE_REFRESH_TOKEN empty in .env. You never need to do this again.")


if __name__ == "__main__":
    main()
