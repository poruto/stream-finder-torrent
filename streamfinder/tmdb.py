"""TMDB API client with an in-memory TTL cache."""

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from . import config

logger = logging.getLogger(__name__)

IMAGE_BASE_URL = "https://image.tmdb.org/t/p"


class TMDBError(Exception):
    """Raised when TMDB is unreachable or returns an error."""


class TMDBClient:
    BASE_URL = "https://api.themoviedb.org/3"
    TIMEOUT = 15

    def __init__(self, api_key: str, language: str = "en-US"):
        self.api_key = api_key
        self.language = language
        self.session = requests.Session()
        self._cache: dict[tuple, tuple] = {}
        self._lock = threading.Lock()

    def _get(
        self, path: str, ttl: int = 3600, language: str | None = None, **params
    ) -> dict[str, Any]:
        params = {
            "api_key": self.api_key,
            "language": language or _request_locale(self.language),
            **{k: v for k, v in params.items() if v is not None},
        }
        key = (path, tuple(sorted(params.items())))
        with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
        try:
            response = self.session.get(
                f"{self.BASE_URL}{path}", params=params, timeout=self.TIMEOUT
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as e:
            # requests puts the full URL (incl. api_key) into the message: keep it out of logs.
            message = re.sub(r"api_key=[^&\s)]+", "api_key=***", str(e))
            raise TMDBError(f"TMDB request {path} failed: {message}") from None
        with self._lock:
            if len(self._cache) > 2000:
                self._cache.clear()
            self._cache[key] = (time.monotonic() + ttl, data)
        return data

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def check_key(self, api_key: str) -> bool:
        """Whether TMDB accepts this API key."""
        try:
            response = self.session.get(
                f"{self.BASE_URL}/configuration",
                params={"api_key": api_key},
                timeout=10,
            )
        except requests.RequestException:
            raise TMDBError("TMDB unreachable") from None
        return response.status_code == 200

    # -- lists ------------------------------------------------------------

    def search_multi(self, query: str, page: int = 1) -> dict[str, Any]:
        return self._get("/search/multi", ttl=600, query=query, page=page)

    def discover(
        self,
        media_type: str,
        genre: int | None = None,
        min_rating: float = 0,
        max_rating: float = 10,
        sort_by: str = "popularity.desc",
        year: int | None = None,
        page: int = 1,
        min_votes: int | None = None,
        decade: str | None = None,
        language: str | None = None,
    ) -> dict[str, Any]:
        if media_type == "tv":
            sort_by = sort_by.replace("primary_release_date", "first_air_date")
        year_param = (
            "primary_release_year" if media_type == "movie" else "first_air_date_year"
        )
        params = {
            "sort_by": sort_by,
            "page": page,
            "vote_average.gte": min_rating,
            "vote_average.lte": max_rating,
            "with_genres": genre,
            year_param: year,
        }
        if "release_date" in sort_by or "first_air_date" in sort_by:
            # Without an upper bound "newest" is full of unreleased announcements.
            date_key = (
                "primary_release_date.lte"
                if media_type == "movie"
                else "first_air_date.lte"
            )
            params[date_key] = time.strftime("%Y-%m-%d")
            params["vote_count.gte"] = 20
        if sort_by.startswith("vote_average"):
            params["vote_count.gte"] = 200  # otherwise 10/10 films with 1 vote dominate
        if min_votes:
            params["vote_count.gte"] = max(params.get("vote_count.gte", 0), min_votes)
        if decade:
            field = (
                "primary_release_date" if media_type == "movie" else "first_air_date"
            )
            if decade == "older":
                params[f"{field}.lte"] = "1979-12-31"
            else:
                params[f"{field}.gte"] = f"{decade}-01-01"
                params[f"{field}.lte"] = min(
                    f"{int(decade) + 9}-12-31", time.strftime("%Y-%m-%d")
                )
        if language:
            params["with_original_language"] = language
        return self._get(f"/discover/{media_type}", ttl=600, **params)

    def listing(self, path: str, page: int = 1) -> dict[str, Any]:
        return self._get(path, ttl=600, page=page)

    def genres(self, media_type: str) -> list[dict[str, Any]]:
        try:
            return self._get(f"/genre/{media_type}/list", ttl=86400).get("genres", [])
        except TMDBError as e:
            logger.warning("%s", e)
            return []

    # -- details ----------------------------------------------------------

    def details(self, media_type: str, tmdb_id: int) -> dict[str, Any]:
        """Movie/TV details incl. credits, ids, trailers and recommendations.

        Adds `english_title` (torrents are named in English) and `imdb_id`.
        """
        data = dict(
            self._get(
                f"/{media_type}/{tmdb_id}",
                append_to_response="credits,external_ids,videos,recommendations,alternative_titles",
                include_video_language=f"{self.language[:2]},en",
            )
        )
        try:
            english = self._get(f"/{media_type}/{tmdb_id}", language="en-US")
            data["english_title"] = english.get("title") or english.get("name") or ""
        except TMDBError:
            data["english_title"] = ""
        try:  # Czech releases are named after the Czech title
            czech = self._get(f"/{media_type}/{tmdb_id}", language="cs-CZ")
            data["czech_title"] = czech.get("title") or czech.get("name") or ""
        except TMDBError:
            data["czech_title"] = ""
        external = data.get("external_ids") or {}
        data["imdb_id"] = data.get("imdb_id") or external.get("imdb_id")
        return data

    def find_imdb(self, imdb_id: str) -> list[dict[str, Any]]:
        """Movies/shows for an IMDb id ('tt0111161')."""
        data = self._get(f"/find/{imdb_id}", ttl=86400, external_source="imdb_id")
        results = [{**r, "media_type": "movie"} for r in data.get("movie_results", [])]
        results += [{**r, "media_type": "tv"} for r in data.get("tv_results", [])]
        return results

    def imdb_ids(self, items: list[tuple[str, int]]) -> dict[tuple[str, int], str]:
        """IMDb ids for many (media_type, tmdb_id) pairs, fetched concurrently and cached for a week."""

        def one(item):
            media_type, tmdb_id = item
            try:
                return item, self._get(
                    f"/{media_type}/{tmdb_id}/external_ids", ttl=7 * 86400
                ).get("imdb_id")
            except TMDBError:
                return item, None

        with ThreadPoolExecutor(max_workers=8) as pool:
            return {item: imdb for item, imdb in pool.map(one, items) if imdb}

    def season(self, tmdb_id: int, season_number: int) -> dict[str, Any]:
        return self._get(f"/tv/{tmdb_id}/season/{season_number}")

    def episode_imdb_id(self, tmdb_id: int, season: int, episode: int) -> str | None:
        """IMDb id of a single episode (OpenSubtitles.org indexes episodes by it)."""
        try:
            data = self._get(
                f"/tv/{tmdb_id}/season/{season}/episode/{episode}/external_ids",
                ttl=86400,
            )
        except TMDBError:
            return None
        return data.get("imdb_id")

    def many(
        self, requests_: dict[str, tuple[str, dict[str, Any]]]
    ) -> dict[str, dict[str, Any]]:
        """Fetch several list endpoints concurrently: {name: (path, params)} -> {name: data}."""
        # Worker threads have no request: pick the visitor's language here, or rows
        # would come back in the server default (Czech titles and posters on the English site).
        language = _request_locale(self.language)
        with ThreadPoolExecutor(max_workers=min(8, len(requests_) or 1)) as pool:
            futures = {
                name: pool.submit(self._get, path, 600, language, **params)
                for name, (path, params) in requests_.items()
            }
        results = {}
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except TMDBError as e:
                logger.warning("%s", e)
                results[name] = {"results": []}
        return results


def _request_locale(default: str) -> str:
    from flask import has_request_context

    if not has_request_context():
        return default
    from .i18n import tmdb_locale

    return tmdb_locale()


def image_url(path: str | None, size: str = "w500") -> str | None:
    return f"{IMAGE_BASE_URL}/{size}{path}" if path else None


def trailer_url(data: dict[str, Any]) -> str | None:
    """YouTube link of the best trailer (local language first, then English)."""
    videos = [
        v
        for v in (data.get("videos") or {}).get("results", [])
        if v.get("site") == "YouTube"
    ]
    videos.sort(
        key=lambda v: (
            v.get("type") != "Trailer",
            not v.get("official"),
            v.get("iso_639_1") != "cs",
        )
    )
    return f"https://www.youtube.com/watch?v={videos[0]['key']}" if videos else None


def match_titles(data: dict[str, Any]) -> list[str]:
    """Every title a torrent for this movie/show might be released under."""
    titles = [
        data.get("english_title"),
        data.get("original_title") or data.get("original_name"),
        data.get("title") or data.get("name"),
        data.get("czech_title"),
    ]
    alternatives = data.get("alternative_titles") or {}
    for alt in alternatives.get("titles") or alternatives.get("results") or []:
        if alt.get("iso_3166_1") in ("US", "GB", "CZ", "SK"):
            titles.append(alt.get("title"))
    return list(dict.fromkeys(t for t in titles if t))


tmdb_client = TMDBClient(config.TMDB_API_KEY, config.LANGUAGE)
