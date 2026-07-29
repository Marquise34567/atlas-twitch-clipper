"""
IRC chat detector.

Connects to Twitch IRC anonymously (justinfan<random>) and reads chat for a
channel. Computes a rolling "hype score" from three signals over a sliding
window (default 10s):

  1. Message-rate spike — msgs/sec vs a baseline. Hype moments flood chat.
  2. Emote density — fraction of messages that are emote-only or contain
     known hype emotes (POG, KEKW, Pog, OMEGALUL, monkaS, etc.).
  3. Keyword hits — "CLIP IT", "NO WAY", "LET'S GO", "POG", profanity spikes,
     all-caps ratio rising.

Thread-safe: `score()` returns the current rolling score; the detector thread
updates it in the background. `stop()` closes the socket.

No external deps — uses the stdlib socket + ssl. Works on free public channels
without any Twitch account (anonymous read).
"""
from __future__ import annotations

import os
import random
import re
import socket
import ssl
import threading
import time
from collections import deque
from typing import Deque

IRC_HOST = "irc.chat.twitch.tv"
IRC_PORT = 6697  # TLS

# Hype emotes (lowercase). Twitch global + common 3rd-party (BTTV/7TV) that
# flood chat during big moments. Not exhaustive — extend to taste.
HYPE_EMOTES = {
    "pog", "poggers", "pogchamp", "kekw", "kekw_", "omegalul", "monkas",
    "pepega", "pepehands", "pepelaugh", "5head", "pogu", "ez", "ezclap",
    "sadge", "catjam", "catjammies", "pausechamp", "modcheck", "weirdchamp",
    "nopers", "pogjaw", "pogeyes", "hypers", "pogyou", "pog7",
    "kappa", "kappahd", "kappajd", "kreygasm", "biblethump",
}

# Keyword spikes — case-insensitive substrings. "clip it" is the strongest
# signal: chatters literally asking for a clip.
HYPE_KEYWORDS = {
    "clip it", "clip that", "clip this", "no way", "no fkin way", "no fucking way",
    "lets go", "let's go", "let's gooo", "holy shit", "holy fk", "omg",
    "insane", "actually insane", "that's insane", "thats insane",
    "wtf", "lmao", "lmfaooo", "carry", "cracked", "demon", "goated",
    "gg", "ez clap", "world record", "pr", "wr",
}

# Profanity / all-caps spike words (lightweight — we don't want a full filter).
SPIKE_WORDS = {"shit", "fuck", "fk", "fkin", "holy", "damn", "ass"}


class ChatDetector:
    def __init__(
        self,
        channel: str,
        window_seconds: float = 10.0,
        baseline_msgs_per_sec: float = 0.5,
        emote_spike_ratio: float = 0.25,
    ) -> None:
        self.channel = channel.lower().lstrip("@")
        self.window = window_seconds
        self.baseline = baseline_msgs_per_sec
        self.emote_spike_ratio = emote_spike_ratio

        # Rolling event log: (timestamp, msg_text)
        self._events: Deque[tuple[float, str]] = deque()
        self._lock = threading.Lock()

        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._connected = False
        self._last_score = 0.0
        self._last_breakdown: dict = {}

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"irc-{self.channel}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def connected(self) -> bool:
        return self._connected

    # ── IRC loop ──────────────────────────────────────────────────────────────
    def _run(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            try:
                self._connect_and_read()
                backoff = 2.0
            except Exception as e:
                self._connected = False
                if self._stop.is_set():
                    break
                print(f"[chat] disconnected ({e}); reconnecting in {backoff:.1f}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _connect_and_read(self) -> None:
        raw = socket.create_connection((IRC_HOST, IRC_PORT), timeout=15)
        ctx = ssl.create_default_context()
        self._sock = ctx.wrap_socket(raw, server_hostname=IRC_HOST)
        self._sock.settimeout(10)

        # Anonymous login: justinfan<random> needs no password for public reads.
        nick = f"justinfan{random.randint(10000, 99999)}"
        self._send(f"CAP REQ :twitch.tv/tags twitch.tv/commands")
        self._send(f"NICK {nick}")
        self._send(f"JOIN #{self.channel}")
        self._connected = True
        print(f"[chat] joined #{self.channel}")

        buf = b""
        while not self._stop.is_set():
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                # Keep-alive: respond to PING; also prune old events here.
                self._prune()
                continue
            if not chunk:
                break
            buf += chunk
            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                self._handle_line(line.decode("utf-8", "replace"))

    def _send(self, msg: str) -> None:
        if self._sock:
            self._sock.sendall((msg + "\r\n").encode("utf-8"))

    def _handle_line(self, line: str) -> None:
        if line.startswith("PING"):
            self._send(line.replace("PING", "PONG"))
            return
        # PRIVMSG with optional tags: @badge-info=...;... :nick!nick@nick.tmi.twitch.tv PRIVMSG #chan :message
        m = re.search(r"PRIVMSG #\S+ :(.*)$", line)
        if not m:
            return
        text = m.group(1).strip()
        # Extract emotes from tags if present (emotes=...)
        has_emote_tag = "emotes=" in line and "emotes=" in line.split("PRIVMSG")[0]
        self._record(text, has_emote_tag)

    # ── scoring ───────────────────────────────────────────────────────────────
    def _record(self, text: str, has_emote_tag: bool) -> None:
        now = time.time()
        with self._lock:
            self._events.append((now, text))
        # Prune opportunistically (also pruned on socket timeout).
        self._prune()

    def _prune(self) -> None:
        cutoff = time.time() - self.window
        with self._lock:
            while self._events and self._events[0][0] < cutoff:
                self._events.popleft()

    def score(self) -> tuple[float, dict]:
        """Return (score, breakdown) for the current window. Score is a unitless
        hype magnitude; the detector loop compares it to CLIP_SCORE_THRESHOLD."""
        self._prune()
        with self._lock:
            events = list(self._events)
        if not events:
            self._last_score = 0.0
            self._last_breakdown = {}
            return 0.0, {}

        n = len(events)
        msgs_per_sec = n / self.window
        # 1) message-rate spike (ratio over baseline, floored at 1)
        rate_ratio = max(0.0, (msgs_per_sec - self.baseline) / max(self.baseline, 0.05))
        rate_score = min(rate_ratio, 8.0)  # cap so one flood doesn't dominate

        # 2) emote density
        emote_hits = 0
        for _, text in events:
            low = text.lower()
            if any(e in low for e in HYPE_EMOTES):
                emote_hits += 1
                continue
        emote_ratio = emote_hits / n
        emote_score = (emote_ratio / self.emote_spike_ratio) if self.emote_spike_ratio else 0.0
        emote_score = min(emote_score, 4.0)

        # 3) keyword + caps + spike-word hits
        kw_hits = 0
        caps_hits = 0
        spike_hits = 0
        for _, text in events:
            low = text.lower()
            if any(k in low for k in HYPE_KEYWORDS):
                kw_hits += 1
            # all-caps message of >=4 chars (excluding emotes/punctuation)
            letters = re.sub(r"[^A-Za-z]", "", text)
            if len(letters) >= 4 and letters.isupper():
                caps_hits += 1
            if any(w in low for w in SPIKE_WORDS):
                spike_hits += 1
        kw_score = min(kw_hits / 3.0, 4.0)
        caps_score = min(caps_hits / 3.0, 3.0)
        spike_score = min(spike_hits / 3.0, 3.0)

        # Weighted: keyword "clip it" is the strongest intent signal.
        total = (
            rate_score * 1.0
            + emote_score * 1.2
            + kw_score * 1.5
            + caps_score * 0.6
            + spike_score * 0.8
        )
        breakdown = {
            "messages": n,
            "msgs_per_sec": round(msgs_per_sec, 2),
            "rate_score": round(rate_score, 2),
            "emote_ratio": round(emote_ratio, 2),
            "emote_score": round(emote_score, 2),
            "kw_hits": kw_hits,
            "kw_score": round(kw_score, 2),
            "caps_hits": caps_hits,
            "spike_hits": spike_hits,
            "total": round(total, 2),
        }
        self._last_score = total
        self._last_breakdown = breakdown
        return total, breakdown
