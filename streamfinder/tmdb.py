"""TMDB API client with an in-memory TTL cache that survives restarts."""

import atexit
import json
import logging
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests

from . import config
from .caching import LRU
from .matching import normalize_title

logger = logging.getLogger(__name__)

IMAGE_BASE_URL = "https://image.tmdb.org/t/p"
# Titles every detail page needs besides the visitor's language: key -> TMDB locale.
TITLE_LANGUAGES = {"english_title": "en-US", "czech_title": "cs-CZ"}
CACHE_SIZE = 2000  # answers kept in memory (the least recently used go first)
SAVE_EVERY = 600  # seconds between saves of a changed cache to disk


class TMDBError(Exception):
    """Raised when TMDB is unreachable or returns an error."""


class TMDBClient:
    BASE_URL = "https://api.themoviedb.org/3"
    TIMEOUT = 15

    def __init__(self, api_key: str, language: str = "en-US"):
        self.api_key = api_key
        self.language = language
        self.session = requests.Session()
        # (path, params) -> (monotonic expiry, wall-clock expiry for the disk, data)
        self._cache: LRU = LRU(CACHE_SIZE)
        self._dirty = False  # changed since the last save
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tmdb")

    @staticmethod
    def _key(path: str, language: str, params: dict[str, Any]) -> tuple:
        # Without the API key: answers don't depend on it, and it stays off the disk.
        params = {"language": language, **params}
        return path, tuple(sorted((k, v) for k, v in params.items() if v is not None))

    def _cached(self, key: tuple) -> dict[str, Any] | None:
        entry = self._cache.get(key)
        return entry[2] if entry and entry[0] > time.monotonic() else None

    def _get(
        self, path: str, ttl: int = 3600, language: str | None = None, **params
    ) -> dict[str, Any]:
        language = language or _request_locale(self.language)
        key = self._key(path, language, params)
        cached = self._cached(key)
        if cached is not None:
            return cached
        params = {
            "api_key": self.api_key,
            "language": language,
            **{k: v for k, v in params.items() if v is not None},
        }
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
        self._cache[key] = (time.monotonic() + ttl, time.time() + ttl, data)
        self._dirty = True
        return data

    def clear_cache(self) -> None:
        self._cache.clear()
        self._dirty = True

    # -- cache on disk ------------------------------------------------------

    def persist(self, path: str) -> None:
        """Keep the cache in `path`: loaded now (in the background), saved every
        few minutes and at exit, so a restarted Raspberry Pi doesn't ask TMDB for
        everything again. Every answer still expires when its TTL says.
        """

        def run():
            self.load(path)
            while True:
                time.sleep(SAVE_EVERY)
                self.save(path)

        threading.Thread(target=run, name="tmdb-cache", daemon=True).start()
        atexit.register(self.save, path)

    def load(self, path: str) -> int:
        """Answers saved by `save` that are still valid; returns how many."""
        now, monotonic, loaded = time.time(), time.monotonic(), 0
        try:
            with open(path, encoding="utf-8") as f:
                saved = json.load(f)
            saved_at = float(saved["saved"])
            for path_, params, expires, data in saved["entries"]:
                # At most the time that was left when saved: right after boot the
                # clock of a Pi without a real-time clock may still be behind.
                left = min(expires - now, expires - saved_at)
                key = (path_, tuple((k, v) for k, v in params))
                if left <= 0 or self._cached(key) is not None:
                    continue  # expired, or fetched again meanwhile
                self._cache[key] = (monotonic + left, now + left, data)
                loaded += 1
        except FileNotFoundError:
            return 0
        except (OSError, ValueError, KeyError, TypeError) as e:
            logger.warning("TMDB cache %s unreadable: %s", path, e)
        logger.info("TMDB cache: %d answers loaded from %s", loaded, path)
        return loaded

    def save(self, path: str) -> None:
        """Write the valid answers to `path` (atomically), if anything changed."""
        if not self._dirty:
            return
        self._dirty = False
        now = time.monotonic()
        entries = [
            [key[0], key[1], wall, data]
            for key, (expires, wall, data) in self._cache.snapshot()
            if expires > now
        ]
        directory = os.path.dirname(os.path.abspath(path))
        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(
                        {"saved": time.time(), "entries": entries},
                        f,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                os.replace(tmp_path, path)
            except BaseException:
                os.remove(tmp_path)
                raise
        except OSError as e:
            self._dirty = True
            logger.warning("TMDB cache not saved to %s: %s", path, e)

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

    def show_summary(self, tmdb_id: int) -> dict[str, Any]:
        """A show's seasons and last/next episode to air (English, cached 6 h)."""
        return self._get(f"/tv/{tmdb_id}", ttl=6 * 3600, language="en-US")

    def season_summary(self, tmdb_id: int, season_number: int) -> dict[str, Any]:
        """A season's episodes with their air dates (English, cached 6 h)."""
        return self._get(
            f"/tv/{tmdb_id}/season/{season_number}", ttl=6 * 3600, language="en-US"
        )

    def recommendations(
        self, media_type: str, tmdb_id: int, language: str | None = None
    ) -> list[dict[str, Any]]:
        """TMDB's recommendations for a title (cached 6 h).

        Pass `language` from worker threads, which have no request to read it from.
        """
        data = self._get(
            f"/{media_type}/{tmdb_id}/recommendations", ttl=6 * 3600, language=language
        )
        return data.get("results") or []

    def collection(self, collection_id: int) -> dict[str, Any]:
        """A film series ("Dune Collection") with its parts (cached a day)."""
        return self._get(f"/collection/{collection_id}", ttl=86400)

    def latest_episode(self, tmdb_id: int) -> dict[str, Any] | None:
        """A show's last aired episode {season, episode, air_date} (specials don't count)."""
        last = self.show_summary(tmdb_id).get("last_episode_to_air") or {}
        if not last.get("season_number") or not last.get("episode_number"):
            return None
        return {
            "season": last["season_number"],
            "episode": last["episode_number"],
            "air_date": last.get("air_date"),
        }

    def details(self, media_type: str, tmdb_id: int) -> dict[str, Any]:
        """Movie/TV details incl. credits, ids, trailers and recommendations.

        Adds `english_title` (torrents are named in English) and `imdb_id`.
        """
        path = f"/{media_type}/{tmdb_id}"
        locale = _request_locale(self.language)
        # English and Czech titles (Czech releases are named after the Czech title) are
        # fetched alongside; the one in the visitor's own language is in `data` already.
        others = {
            name: self._pool.submit(self._get, path, 3600, language)
            for name, language in TITLE_LANGUAGES.items()
            if language != locale
        }
        data = dict(
            self._get(
                path,
                language=locale,
                append_to_response="credits,external_ids,videos,recommendations,alternative_titles",
                include_video_language=f"{self.language[:2]},en",
            )
        )
        for name in TITLE_LANGUAGES:
            try:
                source = others[name].result() if name in others else data
                data[name] = source.get("title") or source.get("name") or ""
            except TMDBError:
                data[name] = ""
        external = data.get("external_ids") or {}
        data["imdb_id"] = data.get("imdb_id") or external.get("imdb_id")
        return data

    def has_namesake(self, media_type: str, tmdb_id: int, titles: list[str]) -> bool:
        """Whether another known movie/show has the same title (remakes, US/UK versions).

        Torrent names of such titles need a year or an IMDb id to tell them apart.
        """
        wanted = {normalize_title(t) for t in titles if t} - {""}
        if not wanted:
            return False
        try:
            data = self._get(
                "/search/multi", ttl=86400, language="en-US", query=titles[0]
            )
        except TMDBError as e:
            logger.info("%s", e)
            return False
        for item in data.get("results") or []:
            kind = item.get("media_type")
            if kind not in ("movie", "tv") or (kind, item.get("id")) == (
                media_type,
                tmdb_id,
            ):
                continue  # people, or the title itself
            if (item.get("vote_count") or 0) < 25:
                continue  # obscure shorts and stubs have no torrents to confuse us
            names = (
                item.get("title") or item.get("name"),
                item.get("original_title") or item.get("original_name"),
            )
            if wanted & {normalize_title(n) for n in names if n}:
                return True
        return False

    def find_imdb(self, imdb_id: str) -> list[dict[str, Any]]:
        """Movies/shows for an IMDb id ('tt0111161')."""
        data = self._get(f"/find/{imdb_id}", ttl=86400, external_source="imdb_id")
        results = [{**r, "media_type": "movie"} for r in data.get("movie_results", [])]
        results += [{**r, "media_type": "tv"} for r in data.get("tv_results", [])]
        return results

    def imdb_ids(self, items: list[tuple[str, int]]) -> dict[tuple[str, int], str]:
        """IMDb ids for many (media_type, tmdb_id) pairs, fetched concurrently and cached for a week."""

        def path(item):
            return f"/{item[0]}/{item[1]}/external_ids"

        def one(item):
            try:
                # The server language, as before: worker threads have no request.
                return item, self._get(path(item), 7 * 86400, self.language).get(
                    "imdb_id"
                )
            except TMDBError:
                return item, None

        # Cached ones are answered here: no threads to start for a page seen before.
        found, missing = {}, []
        for item in items:
            cached = self._cached(self._key(path(item), self.language, {}))
            if cached is None:
                missing.append(item)
            elif cached.get("imdb_id"):
                found[item] = cached["imdb_id"]
        if missing:
            with ThreadPoolExecutor(max_workers=min(8, len(missing))) as pool:
                found.update(
                    (item, imdb) for item, imdb in pool.map(one, missing) if imdb
                )
        return {item: found[item] for item in items if item in found}

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
        # Cached rows are taken right away; only the others are fetched in threads.
        cached = {
            name: self._cached(self._key(path, language, params))
            for name, (path, params) in requests_.items()
        }
        missing = {
            name: requests_[name] for name, data in cached.items() if data is None
        }
        futures = {}
        if missing:
            with ThreadPoolExecutor(max_workers=min(8, len(missing))) as pool:
                futures = {
                    name: pool.submit(self._get, path, 600, language, **params)
                    for name, (path, params) in missing.items()
                }
        results = {}
        for name in requests_:
            if name not in futures:
                results[name] = cached[name]
                continue
            try:
                results[name] = futures[name].result()
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
