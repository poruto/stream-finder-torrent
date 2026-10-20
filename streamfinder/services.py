"""Long-lived service objects shared by all requests."""

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
from .torrserver import TorrServerClient
from .transcode import Transcoder


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


def services() -> Services:
    return current_app.extensions["streamfinder"]
