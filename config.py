"""Configuration module for Flask streaming application."""

import os
from typing import Dict, List, Any
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


class Config:
    """Application configuration class."""

    # TMDB API Configuration
    TMDB_API_KEY: str = os.getenv("TMDB_API_KEY", "").strip()
    LANGUAGE: str = os.getenv("LANGUAGE", "cs-CZ")

    # TorrServer Configuration
    TORRSERVER_URL: str = os.getenv("TORRSERVER_URL", "http://127.0.0.1:8090").rstrip("/")
    TORRSERVER_STREAM_PATH: str = os.getenv("TORRSERVER_STREAM_PATH", "/stream")

    # Torrent API Settings
    TORRENT_TIMEOUT: int = int(os.getenv("TORRENT_TIMEOUT", "5"))
    MAX_TORRENT_RESULTS: int = int(os.getenv("MAX_TORRENT_RESULTS", "20"))

    # Subtitle Settings
    SUBTITLES_ENABLED: bool = os.getenv("SUBTITLES_ENABLED", "true").lower() == "true"
    SUBTITLE_LANGUAGES: List[str] = os.getenv("SUBTITLE_LANGUAGES", "cs,en,sk").split(",")
    OPENSUBTITLES_API_KEY: str = os.getenv("OPENSUBTITLES_API_KEY", "")

    # Available torrent API sources
    TORRENT_API_SOURCES: Dict[str, Dict[str, Any]] = {
        "yts": {
            "enabled": True,
            "name": "YTS Movies",
            "url": "https://yts.mx/api/v2/list_movies.json",
            "type": "movies"
        },
        "rarbg": {
            "enabled": False,
            "name": "RARBG",
            "url": "https://torrentapi.org/pubapi_v2.php",
            "type": "both"
        },
        "thepiratebay": {
            "enabled": False,
            "name": "The Pirate Bay",
            "url": "https://apibay.org/q.php",
            "type": "both"
        }
    }

    # Flask Configuration
    DEBUG: bool = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    SECRET_KEY: str = os.getenv("SECRET_KEY", "dev-secret-key-change-in-production")

    # Cache Settings
    CACHE_TIMEOUT: int = int(os.getenv("CACHE_TIMEOUT", "300"))  # 5 minutes

    # Request Settings
    REQUEST_TIMEOUT: int = int(os.getenv("REQUEST_TIMEOUT", "10"))
    MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))

    @classmethod
    def validate(cls) -> None:
        """Validate configuration settings."""
        if not cls.TMDB_API_KEY:
            raise ValueError("TMDB_API_KEY is required in environment variables")

        if cls.TORRENT_TIMEOUT < 1 or cls.TORRENT_TIMEOUT > 30:
            raise ValueError("TORRENT_TIMEOUT must be between 1 and 30 seconds")

        if cls.MAX_TORRENT_RESULTS < 1 or cls.MAX_TORRENT_RESULTS > 100:
            raise ValueError("MAX_TORRENT_RESULTS must be between 1 and 100")

    @classmethod
    def get_enabled_torrent_sources(cls) -> Dict[str, Dict[str, Any]]:
        """Get only enabled torrent sources."""
        return {
            key: value for key, value in cls.TORRENT_API_SOURCES.items()
            if value.get("enabled", False)
        }

    @classmethod
    def get_torrent_source_by_type(cls, source_type: str) -> Dict[str, Dict[str, Any]]:
        """Get torrent sources by type (movies, tv, both)."""
        return {
            key: value for key, value in cls.get_enabled_torrent_sources().items()
            if value.get("type") in [source_type, "both"]
        }


class DevelopmentConfig(Config):
    """Development configuration."""
    DEBUG = True


class ProductionConfig(Config):
    """Production configuration."""
    DEBUG = False
    SECRET_KEY = os.getenv("SECRET_KEY", None)

    @classmethod
    def validate(cls) -> None:
        """Validate production configuration."""
        super().validate()
        if not cls.SECRET_KEY or cls.SECRET_KEY == "dev-secret-key-change-in-production":
            raise ValueError("SECRET_KEY must be set for production")


class TestingConfig(Config):
    """Testing configuration."""
    DEBUG = True
    TESTING = True
    TMDB_API_KEY = "test-api-key"


# Configuration mapping
config_map = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'testing': TestingConfig,
    'default': DevelopmentConfig
}


def get_config(config_name: str = None) -> type[Config]:
    """Get configuration class by name."""
    if config_name is None:
        config_name = os.getenv('FLASK_ENV', 'default')

    return config_map.get(config_name, DevelopmentConfig)


# Initialize configuration
config = get_config()

# Validate configuration on import
try:
    config.validate()
except ValueError as e:
    print(f"Configuration error: {e}")
    # In production, you might want to raise the exception
    # raise e

# Export commonly used variables for backward compatibility
TMDB_API_KEY = config.TMDB_API_KEY
LANGUAGE = config.LANGUAGE
TORRSERVER_URL = config.TORRSERVER_URL
TORRSERVER_STREAM_PATH = config.TORRSERVER_STREAM_PATH
TORRENT_TIMEOUT = config.TORRENT_TIMEOUT
MAX_TORRENT_RESULTS = config.MAX_TORRENT_RESULTS
SUBTITLES_ENABLED = config.SUBTITLES_ENABLED
SUBTITLE_LANGUAGES = config.SUBTITLE_LANGUAGES
OPENSUBTITLES_API_KEY = config.OPENSUBTITLES_API_KEY
TORRENT_API_SOURCES = config.TORRENT_API_SOURCES