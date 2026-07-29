"""
Twitch Helix API wrapper.

Two token types:
  - App access token (client credentials): used to read public stream/category data.
  - User access token (OAuth code flow): required for clips:edit. The broadcaster
    completes OAuth once; we store access + refresh tokens in tokens.json and refresh
    as needed.

Token storage: tokens.json next to this file (gitignored via .env pattern? no —
we add it to .gitignore explicitly). Holds the broadcaster's user tokens.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import requests

# Load .env on import so the modules work standalone (not just via bot.py).
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except Exception:
    pass

HELIX = "https://api.twitch.tv/helix"
OAUTH_BASE = "https://id.twitch.tv/oauth2"

TOKEN_FILE = Path(__file__).parent / "tokens.json"

# Scopes needed: create clips. Stream/category reads use the app access token
# (no user scope needed for public stream data), so clips:edit is the only
# user scope required.
USER_SCOPES = ["clips:edit"]


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def client_id() -> str:
    return _env("TWITCH_CLIENT_ID")


def client_secret() -> str:
    return _env("TWITCH_CLIENT_SECRET")


def redirect_uri() -> str:
    return _env("TWITCH_REDIRECT_URI", "http://localhost:8000/auth/twitch/callback")


# ── App access token (client credentials) ────────────────────────────────────
class AppToken:
    """Caches a single app access token; refreshes when it expires."""

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._expires_at: float = 0.0

    def get(self) -> str:
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        return self._refresh()

    def _refresh(self) -> str:
        r = requests.post(
            f"{OAUTH_BASE}/token",
            params={
                "client_id": client_id(),
                "client_secret": client_secret(),
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        self._token = data["access_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 3600))
        return self._token  # type: ignore[return-value]


_app_token = AppToken()


def _helix_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Client-Id": client_id(),
    }


# ── Public stream reads (app access) ──────────────────────────────────────────
def get_stream(user_login: str) -> Optional[dict]:
    """Return the stream object if live, else None.
    https://dev.twitch.tv/docs/api/reference/#get-streams
    """
    r = requests.get(
        f"{HELIX}/streams",
        params={"user_login": user_login},
        headers=_helix_headers(_app_token.get()),
        timeout=15,
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    return data[0] if data else None


def get_user(user_login: str) -> Optional[dict]:
    """Resolve login -> user object (id, login, display_name)."""
    r = requests.get(
        f"{HELIX}/users",
        params={"login": user_login},
        headers=_helix_headers(_app_token.get()),
        timeout=15,
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    return data[0] if data else None


def get_user_from_token(token: str) -> Optional[dict]:
    """Get the user that owns this access token (calls /helix/users with no
    login param, which returns the token's owner)."""
    r = requests.get(
        f"{HELIX}/users",
        headers=_helix_headers(token),
        timeout=15,
    )
    r.raise_for_status()
    data = r.json().get("data", [])
    return data[0] if data else None


# ── User OAuth flow (for clips:edit) ──────────────────────────────────────────
def authorize_url(state: str = "atlas") -> str:
    params = {
        "client_id": client_id(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(USER_SCOPES),
        "state": state,
    }
    return f"https://id.twitch.tv/oauth2/authorize?{urllib.parse.urlencode(params)}"


def exchange_code(code: str) -> dict:
    r = requests.post(
        f"{OAUTH_BASE}/token",
        data={
            "client_id": client_id(),
            "client_secret": client_secret(),
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri(),
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def refresh_user_token(refresh_token: str) -> dict:
    r = requests.post(
        f"{OAUTH_BASE}/token",
        data={
            "client_id": client_id(),
            "client_secret": client_secret(),
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def save_user_tokens(tokens: dict) -> None:
    payload = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens.get("refresh_token", ""),
        "expires_at": time.time() + int(tokens.get("expires_in", 3600)),
        "scope": tokens.get("scope", " ".join(USER_SCOPES)),
    }
    TOKEN_FILE.write_text(json.dumps(payload, indent=2))


def load_user_tokens() -> Optional[dict]:
    if not TOKEN_FILE.exists():
        return None
    return json.loads(TOKEN_FILE.read_text())


def get_valid_user_token() -> Optional[str]:
    """Return a non-expired user access token, refreshing if needed.
    Returns None if no tokens are stored (broadcaster hasn't OAuth'd yet).
    """
    stored = load_user_tokens()
    if not stored:
        return None
    # Refresh if within 60s of expiry (or already expired).
    if time.time() >= stored.get("expires_at", 0) - 60:
        try:
            refreshed = refresh_user_token(stored["refresh_token"])
            save_user_tokens(refreshed)
            return refreshed["access_token"]
        except Exception as e:
            print(f"[twitch] token refresh failed: {e}")
            return None
    return stored["access_token"]


# ── Create clip (user access, clips:edit) ────────────────────────────────────
def create_clip(broadcaster_id: str, has_delay: bool = False) -> Optional[dict]:
    """POST /helix/clips — captures the trailing ~30s. Returns clip object or None.
    Requires a user access token with clips:edit for the broadcaster.
    """
    token = get_valid_user_token()
    if not token:
        print("[twitch] cannot clip — no user token (broadcaster hasn't OAuth'd). Visit /auth/twitch")
        return None
    r = requests.post(
        f"{HELIX}/clips",
        params={"broadcaster_id": broadcaster_id, "has_delay": str(has_delay).lower()},
        headers=_helix_headers(token),
        timeout=15,
    )
    if r.status_code == 401:
        # Token may have died early — force a refresh and retry once.
        stored = load_user_tokens()
        if stored:
            try:
                refreshed = refresh_user_token(stored["refresh_token"])
                save_user_tokens(refreshed)
                r = requests.post(
                    f"{HELIX}/clips",
                    params={"broadcaster_id": broadcaster_id, "has_delay": str(has_delay).lower()},
                    headers=_helix_headers(refreshed["access_token"]),
                    timeout=15,
                )
            except Exception as e:
                print(f"[twitch] retry-refresh failed: {e}")
                return None
    if not r.ok:
        print(f"[twitch] create_clip failed {r.status_code}: {r.text}")
        return None
    data = r.json().get("data", [])
    return data[0] if data else None
