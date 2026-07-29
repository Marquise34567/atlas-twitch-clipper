FROM python:3.11-slim

# ffmpeg only needed if ENABLE_AUDIO_DETECTOR=true; install it anyway so the
# image works either way without a rebuild.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000
CMD ["python", "bot.py"]
