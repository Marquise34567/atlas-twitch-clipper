"""
Optional audio loudness detector.

Pulls the live Twitch HLS stream via ffmpeg (audio-only, mono, low rate) and
computes a rolling RMS loudness. A sudden spike above the rolling baseline
indicates a reaction/yell — a strong "crazy moment" signal independent of chat.

Heavier than chat-only (continuous ffmpeg process + audio decode), so it's
opt-in via ENABLE_AUDIO_DETECTOR=true. Default off.

Uses ffmpeg's `ebur128` filter output (parsed from stderr) for loudness, which
is more accurate than raw RMS for perceptual spike detection.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections import deque
from typing import Deque

# Rolling baseline window (seconds of loudness samples, ~1 sample/s from ebur128)
BASELINE_WINDOW = 30.0


class AudioDetector:
    def __init__(self, channel: str, spike_db: float = 8.0) -> None:
        self.channel = channel.lower().lstrip("@")
        self.spike_db = spike_db
        self._samples: Deque[tuple[float, float]] = deque()  # (t, loudness_lufs)
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_score = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"audio-{self.channel}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)

    def _stream_url(self) -> str:
        """Get the direct HLS stream URL using yt-dlp.
        ffmpeg can't open twitch.tv/<channel> directly — it needs the m3u8."""
        try:
            proc = subprocess.run(
                ["python", "-m", "yt_dlp", "-g", "-f", "b", f"https://twitch.tv/{self.channel}"],
                capture_output=True, text=True, timeout=15,
            )
            url = proc.stdout.strip().split("\n")[0].strip()
            if url and url.startswith("http"):
                return url
        except Exception as e:
            print(f"[audio] yt-dlp failed: {e}")
        return f"https://twitch.tv/{self.channel}"

    def _run(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                self._ingest()
                backoff = 5.0
            except Exception as e:
                if self._stop.is_set():
                    break
                print(f"[audio] ingest failed ({e}); retrying in {backoff:.1f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)

    def _ingest(self) -> None:
        # -re (realtime), audio-only, mono 16kHz, ebur128 filter prints loudness to stderr.
        cmd = [
            "ffmpeg", "-hide_banner", "-nostats",
            "-re",
            "-i", self._stream_url(),
            "-vn",  # no video
            "-ac", "1", "-ar", "16000",
            "-af", "ebur128=peak=true",
            "-f", "null", "-",
        ]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        # ebur128 prints lines like:
        #   I: -23.0 LUFS  (momentary + peak)
        # We parse the momentary loudness "M:" field.
        loud_re = re.compile(r"M:\s*(-?\d+\.\d+)")
        assert self._proc.stderr is not None
        for line in self._proc.stderr:
            if self._stop.is_set():
                break
            m = loud_re.search(line)
            if not m:
                continue
            try:
                loud = float(m.group(1))
            except ValueError:
                continue
            now = time.time()
            with self._lock:
                self._samples.append((now, loud))
                cutoff = now - BASELINE_WINDOW
                while self._samples and self._samples[0][0] < cutoff:
                    self._samples.popleft()
            self._compute_score(loud)

    def _compute_score(self, current_loud: float) -> None:
        with self._lock:
            samples = [l for _, l in self._samples[:-1]]  # exclude current
        if len(samples) < 5:
            self._last_score = 0.0
            return
        # Baseline = median of recent samples (robust to one-off spikes).
        srt = sorted(samples)
        baseline = srt[len(srt) // 2]
        spike = current_loud - baseline  # positive = louder than baseline
        if spike < self.spike_db:
            self._last_score = 0.0
            return
        # Score scales with how far above the spike threshold we are.
        self._last_score = min((spike - self.spike_db) / 4.0, 4.0) + 1.0

    def score(self) -> float:
        return self._last_score
