"""
Atlas Twitch Clipper — main orchestrator.

A single-user bot that watches one Twitch channel and auto-clips "crazy
moments" (chat hype spikes + optional audio loudness). The channel is
discovered from the OAuth'd user's own token — no hardcoded channel needed.

API:
  GET  /health
  GET  /auth/twitch             -> redirect to Twitch OAuth
  GET  /auth/twitch/callback    -> exchanges code, stores tokens, redirects to frontend
  GET  /api/clipper/status      -> bot state (connected? watching? last clip?)
  POST /api/clipper/start       -> start watching the OAuth'd user's channel
  POST /api/clipper/stop        -> stop watching

The bot does NOT auto-start on boot. The user must:
  1. Visit /auth/twitch (or click "Connect Twitch" on the frontend)
  2. Call /api/clipper/start (or click "Start Auto-Clipper" on the frontend)
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from chat_detector import ChatDetector
from twitch_api import (
    authorize_url,
    create_clip,
    exchange_code,
    get_stream,
    get_stream_check,
    get_user,
    get_user_from_token,
    get_valid_user_token,
    load_user_tokens,
    save_user_tokens,
)

POLL_INTERVAL = 30.0


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _bool(key: str, default: bool = False) -> bool:
    return os.environ.get(key, "").lower() in ("1", "true", "yes") if os.environ.get(key) else default


class ClipperBot:
    """Manages the watch loop for a single channel. Start/stop on demand."""

    def __init__(self) -> None:
        self.category_filter = _env("TWITCH_CATEGORY_FILTER")
        self.threshold = float(_env("CLIP_SCORE_THRESHOLD", "3.0"))
        self.cooldown = float(_env("CLIP_COOLDOWN_SECONDS", "30"))
        self.enable_audio = _bool("ENABLE_AUDIO_DETECTOR", False)
        self.audio_spike_db = float(_env("AUDIO_LOUDNESS_SPIKE_DB", "8.0"))
        self.enable_voice = _bool("ENABLE_VOICE_COMMANDS", True)

        self.channel: Optional[str] = None
        self.broadcaster_id: Optional[str] = None
        self.chat: Optional[ChatDetector] = None
        self.audio = None
        self.voice = None
        self.detector = None
        self.stream_state: dict = {}
        self._stream_error_count = 0
        self._stream_last_error = ""
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False

        if not _env("TWITCH_CLIENT_ID") or not _env("TWITCH_CLIENT_SECRET"):
            raise RuntimeError("TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET not set")

    # ── lifecycle ──────────────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._running

    def start_watching(self) -> bool:
        """Resolve the channel from the user's OAuth token, then start polling."""
        token = get_valid_user_token()
        if not token:
            print("[bot] cannot start — no user token (broadcaster hasn't OAuth'd)")
            return False
        user = get_user_from_token(token)
        if not user:
            print("[bot] could not resolve user from token")
            return False
        self.channel = user["login"]
        self.broadcaster_id = user["id"]
        self._running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="clipper-bot", daemon=True)
        self._thread.start()
        print(f"[bot] started watching #{self.channel} (id={self.broadcaster_id})")
        return True

    def stop_watching(self) -> None:
        self._running = False
        self._stop.set()
        self._stop_detectors()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        self.channel = None
        self.broadcaster_id = None
        self.stream_state = {}
        print("[bot] stopped")

    def _run(self) -> None:
        while not self._stop.is_set() and self._running:
            try:
                self._tick()
            except Exception as e:
                print(f"[bot] tick error: {e}")
            self._stop.wait(POLL_INTERVAL)

    def _tick(self) -> None:
        if not self.channel:
            return
        result = get_stream_check(self.channel)

        # ── API error: don't assume offline, keep detectors running ──
        if result.get("live") is None:
            self._stream_error_count += 1
            err = result.get("error", "unknown error")
            self._stream_last_error = err
            print(f"[bot] stream check failed ({self._stream_error_count}x): {err}")
            # Only after 5 consecutive failures do we give up and stop detectors.
            if self._stream_error_count >= 5 and self.chat:
                print(f"[bot] #{self.channel} — too many stream check failures, stopping detectors")
                self._stop_detectors()
                self.stream_state = {"live": False, "error": err}
            else:
                # Keep previous state but surface the error so the UI can show it.
                prev_live = self.stream_state.get("live")
                self.stream_state = {
                    "live": prev_live if prev_live is not None else False,
                    "error": err,
                    "error_count": self._stream_error_count,
                }
            return

        # ── API succeeded — reset error counter ──
        if self._stream_error_count > 0:
            print(f"[bot] stream check recovered after {self._stream_error_count} failures")
        self._stream_error_count = 0
        self._stream_last_error = ""

        stream = result.get("stream")
        if not stream:
            if self.chat:
                print(f"[bot] #{self.channel} went offline — stopping detectors")
                self._stop_detectors()
            self.stream_state = {"live": False}
            return

        game = stream.get("game_name", "")
        if self.category_filter and game.lower() != self.category_filter.lower():
            if self.chat:
                print(f"[bot] category '{game}' != filter '{self.category_filter}' — stopping")
                self._stop_detectors()
            self.stream_state = {"live": True, "game": game, "filtered_out": True}
            return

        if not self.chat:
            print(f"[bot] #{self.channel} is LIVE playing '{game}' — starting detectors")
            self._start_detectors()
        self.stream_state = {"live": True, "game": game, "viewer_count": stream.get("viewer_count")}

    def _start_detectors(self) -> None:
        self.chat = ChatDetector(
            self.channel or "",
            window_seconds=float(_env("CHAT_WINDOW_SECONDS", "10")),
            baseline_msgs_per_sec=float(_env("CHAT_BASELINE_MESSAGES_PER_SEC", "0.5")),
            emote_spike_ratio=float(_env("EMOTE_SPIKE_RATIO", "0.25")),
        )
        self.chat.start()
        if self.enable_audio:
            try:
                from audio_detector import AudioDetector
                self.audio = AudioDetector(self.channel or "", spike_db=self.audio_spike_db)
                self.audio.start()
            except Exception as e:
                print(f"[bot] audio detector disabled ({e})")
                self.audio = None
        if self.enable_voice:
            try:
                from voice_command_detector import VoiceCommandDetector
                self.voice = VoiceCommandDetector(self.channel or "")
                self.voice.start()
                print(f"[bot] voice command detector enabled — say 'clip it' to trigger a clip")
            except Exception as e:
                print(f"[bot] voice command detector disabled ({e})")
                self.voice = None
        from detector import DetectorLoop
        self.detector = DetectorLoop(
            broadcaster_id=self.broadcaster_id or "",
            chat=self.chat,
            audio=self.audio,
            voice=self.voice,
            threshold=self.threshold,
            cooldown=self.cooldown,
        )
        self.detector.start()

    def _stop_detectors(self) -> None:
        if self.detector:
            self.detector.stop()
            self.detector = None
        if self.audio:
            self.audio.stop()
            self.audio = None
        if self.voice:
            self.voice.stop()
            self.voice = None
        if self.chat:
            self.chat.stop()
            self.chat = None

    # ── status ───────────────────────────────────────────────────────────────
    def status(self) -> dict:
        chat_score = 0.0
        chat_breakdown = {}
        if self.chat:
            chat_score, chat_breakdown = self.chat.score()
        return {
            "running": self._running,
            "channel": self.channel,
            "category_filter": self.category_filter or None,
            "stream": self.stream_state,
            "stream_error": self._stream_last_error or None,
            "stream_error_count": self._stream_error_count,
            "broadcaster_id": self.broadcaster_id,
            "chat_connected": bool(self.chat and self.chat.connected),
            "chat_score": round(chat_score, 2),
            "chat_breakdown": chat_breakdown,
            "audio_enabled": bool(self.audio),
            "voice_enabled": bool(self.voice),
            "voice_connected": bool(self.voice and self.voice.connected),
            "voice_last_transcript": self.voice.last_transcript if self.voice else "",
            "threshold": self.threshold,
            "cooldown": self.cooldown,
            "dry_run": _bool("DRY_RUN", False),
            "has_user_token": bool(get_valid_user_token()),
            "last_clip": self.detector.last_clip if self.detector else None,
        }


# ── FastAPI app ───────────────────────────────────────────────────────────────
bot: Optional[ClipperBot] = None

# Frontend URL to redirect back to after OAuth (with a success flag).
FRONTEND_URL = _env("FRONTEND_URL", "https://www.autoeditor.app")
OAUTH_REDIRECT_BACK = f"{FRONTEND_URL}/atlas-clips?twitch_connected=1"


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    global bot
    bot = ClipperBot()
    # Auto-start: if we have a valid token, start watching immediately.
    # This handles Render free tier spin-downs — when the process restarts,
    # the bot automatically resumes watching without user intervention.
    if _bool("AUTO_START", True):
        token = get_valid_user_token()
        if token:
            print("[bot] auto-starting (AUTO_START=true + token available)")
            bot.start_watching()
        else:
            print("[bot] no token — auto-start skipped (visit /auth/twitch)")
    yield
    if bot:
        bot.stop_watching()


app = FastAPI(title="Atlas Twitch Clipper", version="1.1.0", lifespan=_lifespan)

# CORS — allow the prod frontend + localhost dev.
allowed = _env("CORS_ORIGINS", f"{FRONTEND_URL},http://localhost:5173,http://localhost:3000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in allowed.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/status")
def status() -> dict:
    return bot.status() if bot else {"ok": False}


@app.get("/auth/twitch")
def auth_twitch() -> RedirectResponse:
    return RedirectResponse(authorize_url())


@app.get("/auth/twitch/url")
def auth_twitch_url() -> dict:
    """Return the Twitch authorize URL as JSON (frontend redirects to it)."""
    return {"url": authorize_url()}


@app.get("/auth/twitch/callback")
def auth_callback(code: str, state: str = "", error: str = "") -> RedirectResponse:
    """Legacy direct callback (kept for local dev). In prod, the callback goes
    to the frontend at /atlas-clips/auth/twitch/callback which POSTs the code
    to /auth/twitch/exchange below."""
    if error:
        return RedirectResponse(f"{FRONTEND_URL}/atlas-clips?twitch_error={error}")
    try:
        tokens = exchange_code(code)
        save_user_tokens(tokens)
    except Exception as e:
        return RedirectResponse(f"{FRONTEND_URL}/atlas-clips?twitch_error=token_exchange_failed")
    return RedirectResponse(OAUTH_REDIRECT_BACK)


@app.post("/auth/twitch/exchange")
async def auth_exchange(request: Request) -> JSONResponse:
    """Receive the OAuth code from the frontend callback route and exchange it
    for tokens. The frontend's redirect_uri is set to the frontend callback
    URL, so the code returned by Twitch is valid for this server-side exchange
    as long as TWITCH_REDIRECT_URI matches."""
    body = await request.json()
    code = body.get("code", "")
    error = body.get("error", "")
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    if not code:
        return JSONResponse({"ok": False, "error": "no code provided"}, status_code=400)
    try:
        tokens = exchange_code(code)
        save_user_tokens(tokens)
    except Exception as e:
        print(f"[bot] token exchange failed: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    # Return the refresh token so the user can save it as an env var on
    # Render (survives redeploys on the free tier with ephemeral disk).
    return JSONResponse({
        "ok": True,
        "refresh_token": tokens.get("refresh_token", ""),
        "instructions": "Save this as TWITCH_REFRESH_TOKEN in your Render env vars to survive redeploys.",
    })


@app.post("/api/clipper/start")
def clipper_start() -> JSONResponse:
    if not bot:
        return JSONResponse({"ok": False, "error": "bot not initialized"}, status_code=500)
    if bot.running:
        return JSONResponse({"ok": True, "already_running": True, **bot.status()})
    ok = bot.start_watching()
    if not ok:
        return JSONResponse({"ok": False, "error": "no user token — visit /auth/twitch first"}, status_code=401)
    return JSONResponse({"ok": True, **bot.status()})


@app.post("/api/clipper/stop")
def clipper_stop() -> JSONResponse:
    if not bot:
        return JSONResponse({"ok": False, "error": "bot not initialized"}, status_code=500)
    if not bot.running:
        return JSONResponse({"ok": True, "already_stopped": True})
    bot.stop_watching()
    return JSONResponse({"ok": True, **bot.status()})


@app.post("/api/clipper/set-token")
async def clipper_set_token(request: Request) -> JSONResponse:
    """Set the OAuth refresh token remotely. This lets the bot survive
    Render free tier redeploys without re-authorization — the caller
    sends the refresh token once, and it's stored in memory + file +
    can be set as an env var for persistence across redeploys."""
    from twitch_api import save_user_tokens, refresh_user_token, _runtime_token_cache
    body = await request.json()
    refresh_token = body.get("refresh_token", "").strip()
    if not refresh_token:
        return JSONResponse({"ok": False, "error": "no refresh_token provided"}, status_code=400)
    try:
        # Immediately refresh to get a fresh access token + new refresh token
        refreshed = refresh_user_token(refresh_token)
        save_user_tokens(refreshed)
        return JSONResponse({
            "ok": True,
            "message": "Token stored. Set TWITCH_REFRESH_TOKEN in Render env vars for permanent persistence.",
            "new_refresh_token": refreshed.get("refresh_token", ""),
        })
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"token refresh failed: {e}"}, status_code=500)


@app.get("/api/clipper/status")
def clipper_status() -> dict:
    return bot.status() if bot else {"ok": False}


@app.get("/api/clipper/clips")
def clipper_clips(limit: int = 20) -> JSONResponse:
    """List recent Twitch clips for the broadcaster. Used by the frontend
    to show auto-created clips on the Atlas Clips page so users can reframe
    them without leaving the app."""
    from twitch_api import list_clips, get_valid_user_token, get_user_from_token
    token = get_valid_user_token()
    if not token:
        return JSONResponse({"ok": False, "error": "no user token", "clips": []}, status_code=401)
    try:
        user = get_user_from_token(token)
        broadcaster_id = user.get("id", "")
        if not broadcaster_id:
            return JSONResponse({"ok": False, "error": "could not resolve broadcaster", "clips": []}, status_code=500)
        clips = list_clips(broadcaster_id, limit=limit)
        return JSONResponse({"ok": True, "clips": clips, "broadcaster_id": broadcaster_id})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e), "clips": []}, status_code=500)


@app.get("/api/clipper/diagnose")
def clipper_diagnose() -> JSONResponse:
    """Run live API diagnostics: app token, user token, stream check.
    Helps troubleshoot 'stream shows offline when actually live' issues."""
    from twitch_api import get_stream_check, get_user_from_token, get_valid_user_token, client_id, client_secret
    import requests as _req

    diag: dict = {"ok": True, "checks": {}}

    # 1) App token
    try:
        r = _req.post("https://id.twitch.tv/oauth2/token", params={
            "client_id": client_id(), "client_secret": client_secret(),
            "grant_type": "client_credentials",
        }, timeout=15)
        diag["checks"]["app_token"] = {
            "status": r.status_code,
            "ok": r.status_code == 200,
            "error": r.text[:200] if r.status_code != 200 else None,
        }
    except Exception as e:
        diag["checks"]["app_token"] = {"ok": False, "error": str(e)}

    # 2) User token + resolve
    token = get_valid_user_token()
    if not token:
        diag["checks"]["user_token"] = {"ok": False, "error": "no stored user token — visit /auth/twitch"}
        diag["ok"] = False
        return JSONResponse(diag)

    try:
        user = get_user_from_token(token)
        if user:
            diag["checks"]["user_token"] = {
                "ok": True, "login": user["login"], "id": user["id"],
                "display_name": user["display_name"],
            }
            channel = user["login"]
        else:
            diag["checks"]["user_token"] = {"ok": False, "error": "token resolved to no user"}
            diag["ok"] = False
            return JSONResponse(diag)
    except Exception as e:
        diag["checks"]["user_token"] = {"ok": False, "error": str(e)}
        diag["ok"] = False
        return JSONResponse(diag)

    # 3) Stream check
    result = get_stream_check(channel)
    if result.get("live") is True:
        s = result["stream"]
        diag["checks"]["stream"] = {
            "ok": True, "live": True, "game": s.get("game_name"),
            "viewers": s.get("viewer_count"), "type": s.get("type"),
        }
    elif result.get("live") is False:
        diag["checks"]["stream"] = {"ok": True, "live": False, "note": "Twitch API says stream is offline"}
    else:
        diag["checks"]["stream"] = {"ok": False, "live": None, "error": result.get("error")}
        diag["ok"] = False

    return JSONResponse(diag)


if __name__ == "__main__":
    port = int(_env("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
