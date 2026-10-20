"""Long-lived service objects shared by all requests."""

from dataclasses import dataclass

from flask import current_app

from . import config
from .imdb import IMDbRatings
from .subtitles import SubtitleService
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

    @classmethod
    def create(cls) -> "Services":
        torrserver = TorrServerClient(
            config.TORRSERVER_URL, config.TORRSERVER_STREAM_PATH
        )
        return cls(
            tmdb=tmdb_client,
            torrserver=torrserver,
            torrents=TorrentSearcher(),
            subtitles=SubtitleService(torrserver),
            imdb=IMDbRatings(config.IMDB_DB_PATH),
            transcoder=Transcoder(),
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


def services() -> Services:
    return current_app.extensions["streamfinder"]
