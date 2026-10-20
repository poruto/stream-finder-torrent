FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg converts AC3/DTS audio (which browsers can't play) to AAC and matches subtitles to speech.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && apt-get clean && rm -r /var/lib/apt/lists

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app.py ./
COPY streamfinder streamfinder

RUN useradd --system --uid 1000 app && mkdir -p /app/cache && chown app /app/cache
USER app

EXPOSE 5000
HEALTHCHECK --interval=60s --timeout=20s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/api/healthz', timeout=4)"

# One worker on purpose: subtitle metadata and caches live in process memory.
# Threads handle concurrency (the /api/play call may wait ~45 s for torrent metadata).
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "16", "--timeout", "120", "app:app"]
