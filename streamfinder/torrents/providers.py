"""Public torrent indexes. Every provider here was verified to answer (Oct 2026).

* YTS          movies, looked up by IMDb id (exact); browser-friendly MP4/AAC encodes
* EZTV         TV episodes and packs, looked up by IMDb id
* APIBay       The Pirate Bay JSON API; many uploads are tagged with an IMDb id
* Knaben       meta-search over 1337x, TPB, RARBG archive, Nyaa and others
* Torrents-CSV DHT-crawled index with live seeder counts
"""

import contextlib
import logging
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..matching import parse_size

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


@dataclass
class RawTorrent:
    """A result as returned by one provider, before merging and ranking."""

    name: str
    info_hash: str
    size: int
    seeders: int
    leechers: int
    provider: str
    magnet: str = ""
    id_match: bool = False  # found by exact IMDb id
    title: str = ""  # name to show when `name` was rewritten for the parser
    imdb: str = ""  # IMDb id the index tagged it with ('tt1234567' / '1234567')


def same_imdb(a: str | None, b: str | None) -> bool | None:
    """Whether two IMDb ids are the same title; None when either is unknown."""
    a, b = (str(x or "").lower().removeprefix("tt") for x in (a, b))
    if not a.isdigit() or not b.isdigit():
        return None
    return int(a) == int(b)


class Provider:
    name = "base"
    media_types = ("movie", "tv")
    health_check: tuple[str, dict[str, Any]] = ("/", {})

    def __init__(self, mirrors: list[str], timeout: int):
        self.mirrors = [m.rstrip("/") for m in mirrors]
        self._mirrors_lock = threading.Lock()
        self.timeout = timeout
        self.session = requests.Session()
        retry = Retry(
            total=1, backoff_factor=0.3, status_forcelist=[429, 500, 502, 503, 504]
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=10))
        self.session.headers["User-Agent"] = USER_AGENT

    def request(
        self, path: str, params: dict[str, Any] | None = None, json: Any = None
    ) -> Any:
        """Call the first mirror that answers; a working mirror moves to the front."""
        last_error = None
        for mirror in list(self.mirrors):
            try:
                if json is None:
                    response = self.session.get(
                        f"{mirror}{path}", params=params, timeout=self.timeout
                    )
                else:
                    response = self.session.post(
                        f"{mirror}{path}", json=json, timeout=self.timeout
                    )
                response.raise_for_status()
                data = response.json()
            except (requests.RequestException, ValueError) as e:
                last_error = e
                logger.debug("%s mirror %s failed: %s", self.name, mirror, e)
                continue
            with self._mirrors_lock:  # parallel queries may promote the same mirror
                if mirror in self.mirrors and mirror != self.mirrors[0]:
                    self.mirrors.remove(mirror)
                    self.mirrors.insert(0, mirror)
            return data
        raise ConnectionError(f"{self.name}: no mirror answered ({last_error})")

    def search(self, query) -> list[RawTorrent]:
        raise NotImplementedError

    def is_available(self) -> bool:
        try:
            path, params = self.health_check
            self.request(path, params)
            return True
        except ConnectionError:
            return False

    @staticmethod
    def each_query(
        queries: Iterable[str], fetch: Callable[[str], list[RawTorrent]]
    ) -> list[RawTorrent]:
        """Run several text queries against one index concurrently."""
        queries = list(queries)
        if len(queries) == 1:
            return fetch(queries[0])
        results: list[RawTorrent] = []
        with ThreadPoolExecutor(max_workers=len(queries)) as pool:
            for future in [pool.submit(fetch, q) for q in queries]:
                try:
                    results.extend(future.result())
                except ConnectionError as e:
                    logger.info("%s", e)
        return results


class YTSProvider(Provider):
    name = "YTS"
    media_types = ("movie",)
    health_check = ("/api/v2/list_movies.json", {"limit": 1})

    def search(self, query) -> list[RawTorrent]:
        by_id = bool(query.imdb_id and not query.free_text)
        term = query.imdb_id if by_id else query.title
        data = self.request(
            "/api/v2/list_movies.json",
            {"query_term": term, "limit": 20, "sort_by": "seeds"},
        )
        results = []
        for movie in (data.get("data") or {}).get("movies") or []:
            for torrent in movie.get("torrents") or []:
                if not torrent.get("hash"):
                    continue
                # Synthesise a scene-style name so the shared parser understands it.
                codec = "x265" if torrent.get("video_codec") == "x265" else "x264"
                source = {"bluray": "BluRay", "web": "WEBRip"}.get(
                    torrent.get("type"), torrent.get("type", "")
                )
                title = movie.get("title_english") or movie.get("title")
                name = f"{title} {movie.get('year')} {torrent.get('quality', '')} {source} {codec} AAC-YTS"
                results.append(
                    RawTorrent(
                        name=name,
                        info_hash=torrent["hash"].lower(),
                        size=torrent.get("size_bytes")
                        or parse_size(torrent.get("size")),
                        seeders=int(torrent.get("seeds") or 0),
                        leechers=int(torrent.get("peers") or 0),
                        provider=self.name,
                        id_match=by_id,
                        imdb=movie.get("imdb_code") or "",
                    )
                )
        return results


class EZTVProvider(Provider):
    name = "EZTV"
    media_types = ("tv",)
    health_check = ("/api/get-torrents", {"limit": 1})
    max_pages = 10

    def search(self, query) -> list[RawTorrent]:
        if not query.imdb_id or query.free_text:
            return []
        imdb = query.imdb_id.removeprefix("tt")
        first = self.request(
            "/api/get-torrents", {"imdb_id": imdb, "limit": 100, "page": 1}
        )
        items = list(first.get("torrents") or [])
        pages = min(self.max_pages, -(-int(first.get("torrents_count") or 0) // 100))
        if pages > 1:
            # EZTV can't filter by season, so fetch the remaining pages concurrently.
            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = [
                    pool.submit(
                        self.request,
                        "/api/get-torrents",
                        {"imdb_id": imdb, "limit": 100, "page": page},
                    )
                    for page in range(2, pages + 1)
                ]
                for future in futures:
                    with contextlib.suppress(
                        ConnectionError
                    ):  # a missing page is not fatal
                        items.extend(future.result().get("torrents") or [])

        results = []
        for item in items:
            season, episode = (
                int(item.get("season") or 0),
                int(item.get("episode") or 0),
            )
            if query.season is not None and season and season != query.season:
                continue
            if query.episode is not None and episode and episode != query.episode:
                continue
            if not item.get("hash"):
                continue
            results.append(
                RawTorrent(
                    name=item.get("title") or item.get("filename", ""),
                    info_hash=item["hash"].lower(),
                    size=int(item.get("size_bytes") or 0),
                    seeders=int(item.get("seeds") or 0),
                    leechers=int(item.get("peers") or 0),
                    provider=self.name,
                    magnet=item.get("magnet_url", ""),
                    imdb=item.get("imdb_id") or "",
                )
            )
        return results


class APIBayProvider(Provider):
    name = "TPB"
    health_check = ("/q.php", {"q": "test", "cat": 0})

    def search(self, query) -> list[RawTorrent]:
        def fetch(text: str) -> list[RawTorrent]:
            results = []
            for item in self.request("/q.php", {"q": text, "cat": 0}) or []:
                info_hash = (item.get("info_hash") or "").lower()
                if not info_hash or set(info_hash) == {"0"}:
                    continue  # "No results returned" placeholder
                if not 200 <= int(item.get("category") or 0) < 300:
                    continue  # video categories only
                results.append(
                    RawTorrent(
                        name=item.get("name", ""),
                        info_hash=info_hash,
                        size=int(item.get("size") or 0),
                        seeders=int(item.get("seeders") or 0),
                        leechers=int(item.get("leechers") or 0),
                        provider=self.name,
                        imdb=item.get("imdb") or "",
                    )
                )
            return results

        return self.each_query(query.text_queries(), fetch)


class KnabenProvider(Provider):
    name = "Knaben"
    health_check = ("/v1", {})
    _categories = {"movie": [3000000], "tv": [2000000]}

    def is_available(self) -> bool:
        try:
            self.request("/v1", json={"query": "test", "size": 1})
            return True
        except ConnectionError:
            return False

    def search(self, query) -> list[RawTorrent]:
        def fetch(text: str) -> list[RawTorrent]:
            payload = {
                "search_type": "100%",
                "search_field": "title",
                "query": text,
                "order_by": "seeders",
                "order_direction": "desc",
                "size": 100,
                "hide_unsafe": True,
                "hide_xxx": True,
            }
            if not query.free_text:
                payload["categories"] = self._categories[query.media_type]
            results = []
            for hit in self.request("/v1", json=payload).get("hits") or []:
                if not hit.get("hash"):
                    continue
                results.append(
                    RawTorrent(
                        name=hit.get("title") or "",
                        info_hash=hit["hash"].lower(),
                        size=int(hit.get("bytes") or 0),
                        seeders=int(hit.get("liveSeeders") or hit.get("seeders") or 0),
                        leechers=int(hit.get("livePeers") or hit.get("peers") or 0),
                        provider=self.name,
                        magnet=hit.get("magnetUrl") or "",
                    )
                )
            return results

        return self.each_query(query.text_queries(), fetch)


class TorrentsCSVProvider(Provider):
    name = "Torrents-CSV"
    health_check = ("/service/search", {"q": "test", "size": 1})

    def search(self, query) -> list[RawTorrent]:
        def fetch(text: str) -> list[RawTorrent]:
            data = self.request("/service/search", {"q": text, "size": 100})
            return [
                RawTorrent(
                    name=item.get("name", ""),
                    info_hash=item["infohash"].lower(),
                    size=int(item.get("size_bytes") or 0),
                    seeders=int(item.get("seeders") or 0),
                    leechers=int(item.get("leechers") or 0),
                    provider=self.name,
                )
                for item in data.get("torrents") or []
                if item.get("infohash")
            ]

        return self.each_query(query.text_queries(), fetch)
