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


def get_app_token() -> Optional[str]:
    """Return the current app access token, or None if unavailable."""
    try:
        return _app_token.get()
    except Exception:
        return None


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


def get_stream_check(user_login: str) -> dict:
    """Robust stream check that distinguishes API errors from genuinely offline.

    Returns a dict with:
      - {"live": True,  "stream": {...}}  — stream is live
      - {"live": False, "stream": None}   — stream is genuinely offline (API succeeded)
      - {"live": None,  "error": "..."}   — API call failed (unknown state, do NOT
                                            assume offline; caller should retry)
    """
    last_err = ""
    for attempt in range(3):
        try:
            r = requests.get(
                f"{HELIX}/streams",
                params={"user_login": user_login},
                headers=_helix_headers(_app_token.get()),
                timeout=15,
            )
            if r.status_code == 401:
                # App token may have gone stale — force refresh and retry.
                _app_token._token = None
                _app_token._expires_at = 0.0
                last_err = f"401 Unauthorized (app token refreshed, retrying)"
                continue
            r.raise_for_status()
            data = r.json().get("data", [])
            if data:
                return {"live": True, "stream": data[0]}
            return {"live": False, "stream": None}
        except requests.exceptions.Timeout:
            last_err = f"request timeout (attempt {attempt+1}/3)"
        except requests.exceptions.ConnectionError as e:
            last_err = f"connection error: {e} (attempt {attempt+1}/3)"
        except Exception as e:
            last_err = f"{type(e).__name__}: {e} (attempt {attempt+1}/3)"
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    return {"live": None, "error": last_err}


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
    if not r.ok:
        # Capture Twitch's actual error message for debugging
        try:
            err_body = r.json()
            msg = err_body.get("message", err_body.get("error", r.text))
        except Exception:
            msg = r.text
        raise RuntimeError(f"Twitch token exchange failed ({r.status_code}): {msg}")
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
    # Write to file (works on persistent storage; no-op on ephemeral)
    try:
        TOKEN_FILE.write_text(json.dumps(payload, indent=2))
    except Exception as e:
        print(f"[twitch] could not write token file: {e}")
    # Also keep in memory so refreshes work even if the file is wiped
    _runtime_token_cache["tokens"] = payload


# In-memory cache for tokens — survives within a process lifetime even
# if the filesystem is wiped (Render free tier ephemeral disk).
_runtime_token_cache: dict = {}


def load_user_tokens() -> Optional[dict]:
    # 1) Check in-memory cache first (always available within the process)
    if _runtime_token_cache.get("tokens"):
        return _runtime_token_cache["tokens"]
    # 2) Check file (persistent storage)
    if TOKEN_FILE.exists():
        try:
            data = json.loads(TOKEN_FILE.read_text())
            _runtime_token_cache["tokens"] = data
            return data
        except Exception:
            pass
    # 3) Fall back to env var (survives redeploys on Render free tier)
    env_refresh = _env("TWITCH_REFRESH_TOKEN")
    env_access = _env("TWITCH_ACCESS_TOKEN")
    if env_refresh or env_access:
        data = {
            "access_token": env_access,
            "refresh_token": env_refresh,
            "expires_at": 0,  # force refresh on first use
            "scope": " ".join(USER_SCOPES),
        }
        _runtime_token_cache["tokens"] = data
        return data
    return None


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
            # Return the existing access token as a last resort — it might
            # still work for a few minutes.
            return stored.get("access_token") or None
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


# ── List clips (user access or app access) ──────────────────────────────────
def list_clips(broadcaster_id: str, limit: int = 20, started_at: Optional[str] = None) -> list[dict]:
    """GET /helix/clips — list recent clips for a broadcaster.
    Returns a list of clip objects with id, title, url, edit_url, thumbnail_url,
    duration, created_at, etc. Sorted by created_at descending (most recent first).
    """
    token = get_valid_user_token()
    if not token:
        token = get_app_token()
    if not token:
        print("[twitch] list_clips — no token available")
        return []
    params: dict = {
        "broadcaster_id": broadcaster_id,
        "first": min(limit, 100),
    }
    if started_at:
        params["started_at"] = started_at
    r = requests.get(
        f"{HELIX}/clips",
        params=params,
        headers=_helix_headers(token),
        timeout=15,
    )
    if not r.ok:
        print(f"[twitch] list_clips failed {r.status_code}: {r.text}")
        return []
    data = r.json().get("data", [])
    data.sort(key=lambda c: c.get("created_at", ""), reverse=True)
    return data[:limit]
