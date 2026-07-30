"""
Voice command detector.

Listens to the live Twitch stream audio and detects spoken trigger phrases
like "clip it", "clip that", "clip this". When detected, sets a flag that
the detector loop checks — same mechanism as the !clip chat command.

This lets a streamer with zero viewers trigger clips just by saying
"clip it" out loud — no chat message needed.

Uses ffmpeg to capture 5-second audio chunks from the stream, converts to
WAV, and runs Google's free speech recognition API (via the
SpeechRecognition library) to transcribe. Lightweight enough for Render's
free tier.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from difflib import SequenceMatcher
from typing import Optional

# Trigger phrases (lowercase). The streamer says these out loud.
# We also do fuzzy matching against these since speech recognition
# often mishears (e.g. "clip it" -> "repeat City", "clip that" -> "crip bat").
TRIGGER_PHRASES = [
    "clip it", "clip that", "clip this", "clip it now",
    "clip that shit", "clip it clip it", "clip",
]

# Words that sound like trigger words (common mishearings by Google STT).
# If the transcript contains any of these, treat it as a trigger.
# Keep this tight — only words that are very close to "clip".
FUZZY_TRIGGER_WORDS = [
    "clip", "klip", "crip", "flip", "blip", "quip",
    "creep", "grip", "click",
]

# Minimum similarity ratio for fuzzy word matching (0.0 to 1.0).
# Higher = stricter (fewer false positives but might miss some real triggers).
FUZZY_THRESHOLD = 0.7

# For phrase matching (whole transcript vs trigger phrase), use a higher
# threshold since we're comparing longer strings.
FUZZY_PHRASE_THRESHOLD = 0.75

# How often to capture + transcribe an audio chunk (seconds).
CAPTURE_INTERVAL = 5.0

# Duration of each audio chunk (seconds).
CAPTURE_DURATION = 5.0


def _fuzzy_match(transcript: str) -> Optional[str]:
    """Check if the transcript matches a trigger phrase, using fuzzy matching.
    Returns the matched phrase if found, None otherwise."""
    low = transcript.lower().strip()

    # 1) Exact substring match (fast path)
    for phrase in TRIGGER_PHRASES:
        if phrase in low:
            return phrase

    # 2) Fuzzy word match — if "clip" or a similar-sounding word is in the
    # transcript, treat it as a trigger. Only match words >= 4 chars to
    # avoid false positives on short words like "up", "it", "on".
    words = low.replace(",", " ").replace(".", " ").split()
    for word in words:
        if len(word) < 4:
            continue
        for trigger_word in FUZZY_TRIGGER_WORDS:
            ratio = SequenceMatcher(None, word, trigger_word).ratio()
            if ratio >= FUZZY_THRESHOLD:
                return f"fuzzy:{word}~{trigger_word}"

    # 3) Fuzzy phrase match — compare the whole transcript against
    # each trigger phrase. Use a higher threshold for phrases.
    for phrase in TRIGGER_PHRASES:
        ratio = SequenceMatcher(None, low, phrase).ratio()
        if ratio >= FUZZY_PHRASE_THRESHOLD:
            return f"fuzzy:{low}~{phrase}"

    return None


class VoiceCommandDetector:
    def __init__(self, channel: str) -> None:
        self.channel = channel.lower().lstrip("@")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # When a trigger phrase is heard, this is set with the timestamp.
        # The detector loop consumes it via consume_voice_command().
        self._voice_command_time: float = 0.0
        self._voice_command_phrase: str = ""
        self._last_transcript: str = ""
        self._connected = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"voice-{self.channel}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def last_transcript(self) -> str:
        return self._last_transcript

    def consume_voice_command(self) -> tuple[bool, str]:
        """Check if a trigger phrase was heard. Returns (triggered, phrase)."""
        if self._voice_command_time > 0:
            phrase = self._voice_command_phrase
            self._voice_command_time = 0.0
            self._voice_command_phrase = ""
            return True, phrase
        return False, ""

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
            print(f"[voice] yt-dlp failed: {e}")
        # Fallback: try the twitch.tv URL directly (may work with some ffmpeg builds)
        return f"https://twitch.tv/{self.channel}"

    def _run(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                self._connected = True
                self._capture_and_transcribe()
                backoff = 5.0
                # Wait between captures
                self._stop.wait(CAPTURE_INTERVAL)
            except Exception as e:
                self._connected = False
                if self._stop.is_set():
                    break
                print(f"[voice] error ({e}); retrying in {backoff:.1f}s")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)
        self._connected = False

    def _capture_and_transcribe(self) -> None:
        """Capture a short audio chunk from the stream and transcribe it."""
        if self._stop.is_set():
            return

        # Use a temp file for the audio chunk
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            # Capture CAPTURE_DURATION seconds of audio from the stream
            cmd = [
                "ffmpeg", "-hide_banner", "-nostats", "-y",
                "-t", str(CAPTURE_DURATION),
                "-i", self._stream_url(),
                "-vn",  # audio only
                "-ac", "1",  # mono
                "-ar", "16000",  # 16kHz — sufficient for speech
                "-f", "wav",
                tmp_path,
            ]
            proc = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=CAPTURE_DURATION + 10,
            )
            if proc.returncode != 0 or self._stop.is_set():
                return

            # Transcribe the audio chunk
            transcript = self._transcribe(tmp_path)
            if transcript:
                self._last_transcript = transcript
                # Use fuzzy matching to catch mishearings
                matched = _fuzzy_match(transcript)
                if matched:
                    print(f"[voice] TRIGGER HEARD: '{transcript}' (matched '{matched}')")
                    self._voice_command_time = time.time()
                    self._voice_command_phrase = matched
                    return
        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    def _transcribe(self, wav_path: str) -> str:
        """Transcribe a WAV file using Google's free speech recognition API."""
        try:
            import speech_recognition as sr
            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_path) as source:
                audio = recognizer.record(source)
            # Google's free STT — no API key needed for limited use
            text = recognizer.recognize_google(audio)
            return str(text)
        except sr.UnknownValueError:
            # Speech was unintelligible — normal, just no transcript
            return ""
        except sr.RequestError as e:
            # API error (rate limit, network) — log but don't crash
            print(f"[voice] STT request error: {e}")
            return ""
        except Exception as e:
            # Don't crash on unexpected errors
            return ""
