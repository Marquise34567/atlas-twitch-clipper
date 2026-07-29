# Atlas Twitch Clipper

A small always-on service that watches a Twitch channel and auto-clips "crazy
moments" — reaction spikes, chat floods, hype emotes, "CLIP IT" keyword spikes,
and (optionally) audio loudness spikes — by calling Twitch's `POST /helix/clips`
endpoint, which captures the trailing ~30 seconds.

## How it works

1. **Polls stream status** every 30s via Helix `GET /streams` (app access token).
   Detects online/offline + the current `game_name` (category).
2. **Category filter** (optional): only clip when the stream's category matches
   `TWITCH_CATEGORY_FILTER`. Blank = clip any category.
3. **When live + matching**, starts two detectors:
   - **Chat (IRC)** — anonymous join, sliding 10s window. Scores message-rate
     spikes, hype-emote density, keyword hits ("clip it", "no way", "let's go"),
     all-caps ratio, and profanity spikes.
   - **Audio (optional)** — pulls the HLS stream via ffmpeg, computes rolling
     loudness (EBU R128), fires on a spike above baseline. Off by default.
4. **Detector loop** combines chat + audio scores; when the combined score
   crosses `CLIP_SCORE_THRESHOLD` and the `CLIP_COOLDOWN_SECONDS` window has
   passed, it calls `POST /helix/clips` → Twitch grabs the trailing 30s.
5. **Cooldown** prevents double-clipping the same moment.

## One-time setup: broadcaster OAuth

Twitch's clip API requires a **user** access token with `clips:edit` — app
credentials alone can't create clips. So the broadcaster (the channel owner)
logs in once:

1. Run the bot locally (or on your deploy target).
2. Visit `http://localhost:8000/auth/twitch` (or your deploy URL).
3. Authorize the app on Twitch.
4. Tokens are saved to `tokens.json` (gitignored). The bot refreshes them
   automatically as they expire.

## Run locally

```bash
cd twitch-clipper
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements.txt
# .env already has your credentials; edit TWITCH_WATCH_CHANNEL if needed
DRY_RUN=1 python bot.py     # detect + log, never fire real clips
```

Then open `http://localhost:8000/auth/twitch` to authorize (once). Remove
`DRY_RUN` to actually create clips.

Status: `http://localhost:8000/status`

## Deploy (Render)

The included `render.yaml` deploys a free web service. Push this folder to a
repo, connect it to Render, and set the env vars in the Render dashboard
(`TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, `TWITCH_WATCH_CHANNEL`). Then visit
`https://<your-service>.onrender.com/auth/twitch` once to authorize.

> Free tiers on Render/Railway spin down after inactivity. For a realtime
> watcher you want a paid "always on" plan, or run it on a small VPS / your own
> machine. The bot reconnects IRC + ffmpeg automatically after any downtime.

## Tuning

| Env var | Default | What it does |
|---|---|---|
| `CLIP_SCORE_THRESHOLD` | `3.0` | Higher = fewer, stronger clips. Lower = more clips. |
| `CLIP_COOLDOWN_SECONDS` | `30` | Min seconds between clips. |
| `CHAT_WINDOW_SECONDS` | `10` | Sliding window for chat scoring. |
| `CHAT_BASELINE_MESSAGES_PER_SEC` | `0.5` | Expected calm chat rate; spikes measured against this. |
| `EMOTE_SPIKE_RATIO` | `0.25` | Emote fraction of chat that counts as a spike. |
| `ENABLE_AUDIO_DETECTOR` | `false` | Turn on ffmpeg loudness detection. |
| `AUDIO_LOUDNESS_SPIKE_DB` | `8.0` | LUFS above baseline that counts as a spike. |
| `TWITCH_CATEGORY_FILTER` | (blank) | Only clip when `game_name` matches (case-insensitive). |
| `DRY_RUN` | (blank) | `1` = detect + log, never call the clip API. |

## Files

- `bot.py` — orchestrator + FastAPI (OAuth callback, /status, /health)
- `twitch_api.py` — Helix wrapper (app token, user OAuth, create_clip)
- `chat_detector.py` — IRC chat hype scorer
- `audio_detector.py` — optional ffmpeg loudness scorer
- `detector.py` — combines signals, applies cooldown, fires clips
