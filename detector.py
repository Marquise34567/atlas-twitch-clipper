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
        threshold: float = 3.0,
        cooldown: float = 30.0,
    ) -> None:
        self.broadcaster_id = broadcaster_id
        self.chat = chat
        self.audio = audio
        self.threshold = threshold
        self.cooldown = cooldown
        self._last_clip_at = 0.0
        self._last_clip: Optional[dict] = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Dry-run mode: compute scores + log, but never call the clip API.
        # Set via env so you can test detection without burning real clips.
        self.dry_run = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

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
        print(f"[detector] started (threshold={self.threshold}, cooldown={self.cooldown}s, dry_run={self.dry_run})")
        while not self._stop.is_set():
            time.sleep(1.0)
            chat_score, breakdown = self.chat.score()
            audio_score = self.audio.score() if self.audio else 0.0
            combined = chat_score + audio_score
            if combined < self.threshold:
                continue
            now = time.time()
            if now - self._last_clip_at < self.cooldown:
                # In cooldown — log but don't fire.
                continue
            self._fire(combined, breakdown, audio_score)

    def _fire(self, combined: float, breakdown: dict, audio_score: float) -> None:
        self._last_clip_at = time.time()
        print(
            f"[detector] CLIP THRESHOLD HIT — combined={combined:.2f} "
            f"(chat={breakdown.get('total', 0):.2f}, audio={audio_score:.2f}) "
            f"breakdown={breakdown}"
        )
        if self.dry_run:
            print("[detector] DRY RUN — not calling create_clip")
            self._last_clip = {"dry_run": True, "score": combined, "breakdown": breakdown}
            return
        clip = create_clip(self.broadcaster_id, has_delay=False)
        if clip:
            print(f"[detector] CLIP CREATED: {clip.get('edit_url') or clip.get('id')}")
            self._last_clip = clip
        else:
            print("[detector] clip creation failed (see twitch_api logs)")
