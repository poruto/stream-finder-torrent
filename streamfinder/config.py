"""Configuration loaded from environment variables (see .env.example)."""

import logging
import os

from dotenv import load_dotenv

# Project root (one level above the package): holds .env and the cache directory.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        logger.warning("%s must be an integer, using %s", name, default)
        return default


def _env_list(name: str, default: str) -> list[str]:
    return [
        item.strip() for item in os.getenv(name, default).split(",") if item.strip()
    ]


# TMDB
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()
LANGUAGE = os.getenv("LANGUAGE", "cs-CZ")

# TorrServer
TORRSERVER_URL = os.getenv("TORRSERVER_URL", "http://127.0.0.1:8090").rstrip("/")
# URL the *browser* uses for streams; empty = same host as this app, TorrServer port.
TORRSERVER_PUBLIC_URL = os.getenv("TORRSERVER_PUBLIC_URL", "").rstrip("/")
TORRSERVER_STREAM_PATH = os.getenv("TORRSERVER_STREAM_PATH", "/stream")

# Torrent search
TORRENT_TIMEOUT = _env_int("TORRENT_TIMEOUT", 10)
MAX_TORRENT_RESULTS = _env_int("MAX_TORRENT_RESULTS", 40)
YTS_MIRRORS = _env_list(
    "YTS_MIRRORS", "https://movies-api.accel.li,https://yts.gg,https://yts.mx"
)
EZTV_MIRRORS = _env_list("EZTV_MIRRORS", "https://eztvx.to,https://eztv.re")
APIBAY_MIRRORS = _env_list("APIBAY_MIRRORS", "https://apibay.org")

KNABEN_MIRRORS = _env_list("KNABEN_MIRRORS", "https://api.knaben.org")
TORRENTS_CSV_MIRRORS = _env_list("TORRENTS_CSV_MIRRORS", "https://torrents-csv.com")

# Subtitles (OpenSubtitles.com: free API key at https://www.opensubtitles.com/consumers)
SUBTITLE_LANGUAGES = _env_list("SUBTITLE_LANGUAGES", "cs,sk,en")
OPENSUBTITLES_API_KEY = os.getenv("OPENSUBTITLES_API_KEY", "").strip()
OPENSUBTITLES_USERNAME = os.getenv("OPENSUBTITLES_USERNAME", "").strip()
OPENSUBTITLES_PASSWORD = os.getenv("OPENSUBTITLES_PASSWORD", "").strip()
OPENSUBTITLES_USER_AGENT = os.getenv("OPENSUBTITLES_USER_AGENT", "StreamFinder v1.0")
SUBTITLE_CACHE_DIR = os.getenv(
    "SUBTITLE_CACHE_DIR", os.path.join(BASE_DIR, "cache", "subtitles")
)

IMDB_DB_PATH = os.getenv(
    "IMDB_DB_PATH", os.path.join(BASE_DIR, "cache", "imdb_ratings.sqlite")
)

# ffmpeg converts unsupported audio (AC3/DTS...) to AAC; found on PATH when empty
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "").strip()
FFPROBE_PATH = os.getenv("FFPROBE_PATH", "").strip()

# Flask
DEBUG = _env_bool("FLASK_DEBUG", False)
SECRET_KEY = os.getenv("SECRET_KEY", "") or os.urandom(32).hex()
HOST = os.getenv("HOST", "127.0.0.1")
PORT = _env_int("PORT", 5000)

if not TMDB_API_KEY:
    logger.error("TMDB_API_KEY is not set - copy .env.example to .env and fill it in")
