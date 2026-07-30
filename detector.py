"""
Detector loop.

Combines chat + (optional) audio scores, applies a cooldown, and fires
POST /helix/clips via twitch_api.create_clip when the combined score crosses
CLIP_SCORE_THRESHOLD.

Runs in its own thread, polled by the orchestrator. Thread-safe `last_clip`
exposes the most recent clip for logging/UI.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Optional

from chat_detector import ChatDetector
from twitch_api import create_clip


class DetectorLoop:
    def __init__(
        self,
        broadcaster_id: str,
        chat: ChatDetector,
        audio=None,  # Optional[AudioDetector]
        voice=None,  # Optional[VoiceCommandDetector]
        threshold: float = 3.0,
        cooldown: float = 30.0,
    ) -> None:
        self.broadcaster_id = broadcaster_id
        self.chat = chat
        self.audio = audio
        self.voice = voice
        self.threshold = threshold
        self.cooldown = cooldown
        self._last_clip_at = 0.0
        self._last_clip: Optional[dict] = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Dry-run mode: compute scores + log, but never call the clip API.
        # Set via env so you can test detection without burning real clips.
        self.dry_run = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
        # Periodic fallback: if no clip has been created in this many seconds
        # while live, create one anyway. Ensures streamers with no viewers /
        # no chat activity still get clips. 0 = disabled.
        self.fallback_interval = float(os.environ.get("FALLBACK_CLIP_INTERVAL", "300"))
        # When the stream went live (set by the bot when detectors start).
        self._live_started_at = time.time()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="detector", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def last_clip(self) -> Optional[dict]:
        return self._last_clip

    def _run(self) -> None:
        print(f"[detector] started (threshold={self.threshold}, cooldown={self.cooldown}s, "
              f"fallback={self.fallback_interval}s, dry_run={self.dry_run})")
        while not self._stop.is_set():
            time.sleep(1.0)
            now = time.time()

            # 1) Check for !clip command — someone typed !clip in chat.
            # This bypasses the threshold but still respects the cooldown.
            triggered, sender = self.chat.consume_clip_command()
            if triggered:
                if now - self._last_clip_at < self.cooldown:
                    print(f"[detector] !clip by {sender} — in cooldown, skipping")
                    continue
                print(f"[detector] !clip command by {sender} — forcing clip")
                self._fire(999.0, {"clip_command": True, "sender": sender}, 0.0,
                           reason="!clip command")
                continue

            # 1b) Check for voice command — streamer said "clip it" out loud.
            if self.voice:
                v_triggered, phrase = self.voice.consume_voice_command()
                if v_triggered:
                    if now - self._last_clip_at < self.cooldown:
                        print(f"[detector] voice '{phrase}' — in cooldown, skipping")
                        continue
                    print(f"[detector] voice command '{phrase}' — forcing clip")
                    self._fire(999.0, {"voice_command": True, "phrase": phrase}, 0.0,
                               reason=f"voice: {phrase}")
                    continue

            # 2) Normal hype detection (chat + audio).
            chat_score, breakdown = self.chat.score()
            audio_score = self.audio.score() if self.audio else 0.0
            combined = chat_score + audio_score
            if combined >= self.threshold:
                if now - self._last_clip_at < self.cooldown:
                    continue
                self._fire(combined, breakdown, audio_score)
                continue

            # 3) Periodic fallback — if no clip has been created in
            # fallback_interval seconds, create one anyway. This ensures
            # streamers with no viewers / no chat activity still get clips.
            if self.fallback_interval > 0 and self._last_clip_at > 0:
                if now - self._last_clip_at >= self.fallback_interval:
                    print(f"[detector] fallback clip — no clip in {self.fallback_interval}s")
                    self._fire(0.0, {"fallback": True}, 0.0, reason="periodic fallback")
                    continue
            elif self.fallback_interval > 0 and self._last_clip_at == 0:
                # First fallback: clip shortly after going live so the
                # streamer gets an initial clip even with zero activity.
                if now - self._live_started_at >= self.fallback_interval:
                    print(f"[detector] fallback clip — first clip after {self.fallback_interval}s live")
                    self._fire(0.0, {"fallback": True}, 0.0, reason="periodic fallback")
                    continue

    def _fire(self, combined: float, breakdown: dict, audio_score: float,
              reason: str = "hype") -> None:
        self._last_clip_at = time.time()
        print(
            f"[detector] CLIP TRIGGERED ({reason}) — combined={combined:.2f} "
            f"(chat={breakdown.get('total', 0):.2f}, audio={audio_score:.2f}) "
            f"breakdown={breakdown}"
        )
        if self.dry_run:
            print("[detector] DRY RUN — not calling create_clip")
            self._last_clip = {"dry_run": True, "score": combined, "breakdown": breakdown, "reason": reason}
            return
        clip = create_clip(self.broadcaster_id, has_delay=False)
        if clip:
            print(f"[detector] CLIP CREATED: {clip.get('edit_url') or clip.get('id')}")
            clip["reason"] = reason
            self._last_clip = clip
        else:
            print("[detector] clip creation failed (see twitch_api logs)")
