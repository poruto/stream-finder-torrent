"""Configuration loaded from environment variables (see .env.example)."""

import logging
import os
import re

from dotenv import load_dotenv

# Project root (one level above the package): holds .env and the cache directory.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# No ${VAR} expansion: passwords and keys are taken exactly as written.
load_dotenv(os.path.join(BASE_DIR, ".env"), interpolate=False)
# API keys entered on the Settings page are stored here (Docker: a file on the data volume).
SETTINGS_FILE = os.getenv("SETTINGS_FILE", os.path.join(BASE_DIR, ".env"))
if os.path.abspath(SETTINGS_FILE) != os.path.abspath(os.path.join(BASE_DIR, ".env")):
    load_dotenv(SETTINGS_FILE, override=True, interpolate=False)

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
# TMDB answers are kept here across restarts (each still expires after its own TTL).
TMDB_CACHE_FILE = os.getenv(
    "TMDB_CACHE_FILE", os.path.join(BASE_DIR, "cache", "tmdb.json")
)

# TorrServer
TORRSERVER_URL = os.getenv("TORRSERVER_URL", "http://127.0.0.1:8090").rstrip("/")
# URL the *browser* uses for streams; empty = same host as this app, TorrServer port.
TORRSERVER_PUBLIC_URL = os.getenv("TORRSERVER_PUBLIC_URL", "").rstrip("/")
TORRSERVER_STREAM_PATH = os.getenv("TORRSERVER_STREAM_PATH", "/stream")
# Network use, sent to TorrServer's settings: upload limit in kB/s ("0" = unlimited,
# "off" = no upload) and how much it downloads ahead ("normal" / "less").
# Empty = leave TorrServer's own settings alone.
TORRSERVER_UPLOAD_LIMIT = os.getenv("TORRSERVER_UPLOAD_LIMIT", "").strip().lower()
TORRSERVER_READ_AHEAD = os.getenv("TORRSERVER_READ_AHEAD", "").strip().lower()

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
# SkTorrent (Czech/Slovak tracker, free account at https://sktorrent.eu): searched when set.
SKTORRENT_USERNAME = os.getenv("SKTORRENT_USERNAME", "").strip()
SKTORRENT_PASSWORD = os.getenv("SKTORRENT_PASSWORD", "").strip()

# Subtitles (OpenSubtitles.com: free API key at https://www.opensubtitles.com/consumers)
SUBTITLE_LANGUAGES = _env_list("SUBTITLE_LANGUAGES", "cs,sk,en")
OPENSUBTITLES_API_KEY = os.getenv("OPENSUBTITLES_API_KEY", "").strip()
OPENSUBTITLES_USERNAME = os.getenv("OPENSUBTITLES_USERNAME", "").strip()
OPENSUBTITLES_PASSWORD = os.getenv("OPENSUBTITLES_PASSWORD", "").strip()
OPENSUBTITLES_USER_AGENT = os.getenv("OPENSUBTITLES_USER_AGENT", "StreamFinder v1.0")
SUBTITLE_CACHE_DIR = os.getenv(
    "SUBTITLE_CACHE_DIR", os.path.join(BASE_DIR, "cache", "subtitles")
)

# AI subtitles: speech-to-text (Whisper) behind an OpenAI-compatible API.
# A free Groq key (https://console.groq.com/keys) is enough; OpenAI keys work too.
TRANSCRIBE_API_KEY = os.getenv("TRANSCRIBE_API_KEY", "").strip()
# Address of a local Whisper server (whisper.cpp from install-whisper.sh: http://127.0.0.1:8178/v1)
TRANSCRIBE_API_URL = os.getenv("TRANSCRIBE_API_URL", "").strip()
# Which one transcribes: "local" (the Whisper server) or "api" (the key); empty = whichever is set
TRANSCRIBE_PROVIDER = os.getenv("TRANSCRIBE_PROVIDER", "").strip()
# Off until the admin switches AI subtitles on in Settings (the installers only prepare them).
TRANSCRIBE_ENABLED = os.getenv("TRANSCRIBE_ENABLED", "false").strip()


def flag(value) -> bool:
    """A setting that may be a bool or a string saved from the Settings page."""
    return str(value).strip().lower() in ("1", "true", "yes", "on")


IMDB_DB_PATH = os.getenv(
    "IMDB_DB_PATH", os.path.join(BASE_DIR, "cache", "imdb_ratings.sqlite")
)

# Phone notifications when a new episode of a followed show can be played (Server
# settings): an ntfy topic URL and/or a Telegram bot. PUBLIC_URL: the app's address in
# the links (empty = this machine's network address).
NOTIFY_NTFY_URL = os.getenv("NOTIFY_NTFY_URL", "").strip()
NOTIFY_TELEGRAM_TOKEN = os.getenv("NOTIFY_TELEGRAM_TOKEN", "").strip()
NOTIFY_TELEGRAM_CHAT = os.getenv("NOTIFY_TELEGRAM_CHAT", "").strip()
PUBLIC_URL = os.getenv("PUBLIC_URL", "").strip()

# ffmpeg converts unsupported audio (AC3/DTS...) to AAC; found on PATH when empty
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "").strip()
FFPROBE_PATH = os.getenv("FFPROBE_PATH", "").strip()

# Flask
DEBUG = _env_bool("FLASK_DEBUG", False)
SECRET_KEY = os.getenv("SECRET_KEY", "") or os.urandom(32).hex()
HOST = os.getenv("HOST", "127.0.0.1")
PORT = _env_int("PORT", 5000)

# Keys that can be changed at runtime from the Settings page.
EDITABLE_KEYS = (
    "TMDB_API_KEY",
    "OPENSUBTITLES_API_KEY",
    "OPENSUBTITLES_USERNAME",
    "OPENSUBTITLES_PASSWORD",
    "TRANSCRIBE_API_KEY",
    "TRANSCRIBE_API_URL",
    "TRANSCRIBE_PROVIDER",
    "TRANSCRIBE_ENABLED",
    "SKTORRENT_USERNAME",
    "SKTORRENT_PASSWORD",
    "TORRSERVER_UPLOAD_LIMIT",
    "TORRSERVER_READ_AHEAD",
)


def _quote(value: str) -> str:
    """`value` written so that python-dotenv reads it back unchanged: plain, in single
    quotes ($ in password hashes, spaces, #) or, with a ' inside, in escaped double quotes."""
    if re.fullmatch(r"[\w.@:/+-]*", value):
        return value
    if "'" not in value:
        return f"'{value}'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save_settings(changes: dict[str, str]) -> None:
    """Write keys to SETTINGS_FILE (other lines are kept) and apply them to this module."""
    lines = []
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            lines = f.read().splitlines()
    pending = dict(changes)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if key in pending:
            lines[i] = f"{key}={_quote(pending.pop(key))}"
    lines += [f"{key}={_quote(value)}" for key, value in pending.items()]
    os.makedirs(os.path.dirname(os.path.abspath(SETTINGS_FILE)), exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, SETTINGS_FILE)
    for key, value in changes.items():
        os.environ[key] = value
        globals()[key] = value


# Admin account for the Settings page (admin / admin until changed there).
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "").strip()
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "").strip()

if not TMDB_API_KEY:
    logger.warning("TMDB_API_KEY is not set yet - enter it on the Settings page")
