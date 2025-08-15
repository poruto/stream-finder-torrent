import os
from dotenv import load_dotenv

load_dotenv()

TMDB_API_KEY = os.getenv("TMDB_API_KEY", "").strip()
LANGUAGE = os.getenv("LANGUAGE", "cs-CZ")

# Opraveno: TORRSERVER_URL (s dvojitým R)
TORRSERVER_URL = os.getenv("TORRSERVER_URL", "http://127.0.0.1:8090").rstrip("/")
TORRSERVER_STREAM_PATH = os.getenv("TORRSERVER_STREAM_PATH", "/stream")

# Torrent API nastavení
TORRENT_TIMEOUT = int(os.getenv("TORRENT_TIMEOUT", "5"))
MAX_TORRENT_RESULTS = int(os.getenv("MAX_TORRENT_RESULTS", "20"))

# Nastavení titulků
SUBTITLES_ENABLED = os.getenv("SUBTITLES_ENABLED", "true").lower() == "true"
SUBTITLE_LANGUAGES = os.getenv("SUBTITLE_LANGUAGES", "cs,en,sk").split(",")
OPENSUBTITLES_API_KEY = os.getenv("OPENSUBTITLES_API_KEY", "")  # Volitelné pro vyšší limity

# Dostupné torrent API zdroje
TORRENT_API_SOURCES = {
    "yts": {
        "enabled": True,
        "name": "YTS Movies",
        "url": "https://yts.mx/api/v2/list_movies.json",
        "type": "movies"
    },
}