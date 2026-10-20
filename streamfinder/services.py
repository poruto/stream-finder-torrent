"""Long-lived service objects shared by all requests."""

import logging
import threading
import time
from dataclasses import dataclass

from flask import current_app

from . import config
from .imdb import IMDbRatings
from .subtitles import SubtitleService
from .subtitles.transcribe import Transcriber
from .subtitles.translate import Translator
from .subtitles.whisper_models import WhisperModels
from .tmdb import TMDBClient, tmdb_client
from .torrents import TorrentSearcher
from .torrserver import TorrServerClient, TorrServerError, network_settings
from .transcode import Transcoder

logger = logging.getLogger(__name__)

NETWORK_RETRY = 10  # seconds between attempts to reach TorrServer for its settings


@dataclass
class Services:
    tmdb: TMDBClient
    torrserver: TorrServerClient
    torrents: TorrentSearcher
    subtitles: SubtitleService
    imdb: IMDbRatings
    transcoder: Transcoder
    transcriber: Transcriber
    translator: Translator
    whisper: WhisperModels

    @classmethod
    def create(cls) -> "Services":
        torrserver = TorrServerClient(
            config.TORRSERVER_URL, config.TORRSERVER_STREAM_PATH
        )
        transcoder = Transcoder()
        transcriber = Transcriber(
            transcoder,
            config.TRANSCRIBE_API_KEY,
            config.TRANSCRIBE_API_URL,
            config.TRANSCRIBE_PROVIDER,
            config.flag(config.TRANSCRIBE_ENABLED),
        )
        transcriber.translator = translator = Translator(transcriber)
        return cls(
            tmdb=tmdb_client,
            torrserver=torrserver,
            torrents=TorrentSearcher(),
            subtitles=SubtitleService(torrserver),
            imdb=IMDbRatings(config.IMDB_DB_PATH),
            transcoder=transcoder,
            transcriber=transcriber,
            translator=translator,
            whisper=WhisperModels(),
        )

    def apply_keys(self) -> None:
        """Use keys changed on the Settings page without a restart."""
        self.tmdb.api_key = config.TMDB_API_KEY
        self.tmdb.clear_cache()
        self.subtitles.api.configure(
            config.OPENSUBTITLES_API_KEY,
            config.OPENSUBTITLES_USERNAME,
            config.OPENSUBTITLES_PASSWORD,
        )
        self.torrents.sktorrent.configure(
            config.SKTORRENT_USERNAME, config.SKTORRENT_PASSWORD
        )
        self.torrents.clear_cache()
        self.transcriber.configure(
            config.TRANSCRIBE_API_KEY,
            config.TRANSCRIBE_API_URL,
            config.TRANSCRIBE_PROVIDER,
            config.flag(config.TRANSCRIBE_ENABLED),
        )

    def apply_network(self, attempts: int = 30) -> bool:
        """Send the upload limit / read-ahead choices to TorrServer; True once it has them.

        TorrServer may start after this app, so it is tried `attempts` times. The choices
        are read again on every attempt: one saved meanwhile must not be overwritten.
        """
        for attempt in range(attempts):
            changes = network_settings(
                config.TORRSERVER_UPLOAD_LIMIT, config.TORRSERVER_READ_AHEAD
            )
            if not changes:
                return True
            try:
                if self.torrserver.update_settings(changes):
                    logger.info("TorrServer settings updated: %s", changes)
                return True
            except (TorrServerError, ValueError) as e:  # offline, or not MatriX
                if attempt + 1 < attempts:
                    time.sleep(NETWORK_RETRY)
                else:
                    logger.warning("TorrServer settings not applied: %s", e)
        return False

    def apply_network_later(self) -> None:
        threading.Thread(
            target=self.apply_network, name="torrserver-settings", daemon=True
        ).start()


def services() -> Services:
    return current_app.extensions["streamfinder"]
