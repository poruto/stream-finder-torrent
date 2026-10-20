"""Torrent search: query all providers in parallel, merge, rank and recommend.

Besides a ranked list, every search returns up to three "picks" so the UI can
offer one obvious Play button and explain the choice:
  recommended   best overall for streaming (quality x availability x browser support)
  best_quality  highest resolution that still has enough seeders
  smallest      a much smaller file for slow connections
CAM/telesync and dead torrents are flagged `low` so the UI can tuck them away
whenever a proper release exists.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from .. import config
from ..matching import (
    MatchTarget,
    ReleaseInfo,
    format_size,
    pack_kind,
    parse_release,
    score_torrent,
)
from .providers import (
    APIBayProvider,
    EZTVProvider,
    KnabenProvider,
    Provider,
    RawTorrent,
    TorrentsCSVProvider,
    YTSProvider,
)

logger = logging.getLogger(__name__)

TRACKERS = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
]
CACHE_TTL = 15 * 60
SEARCH_DEADLINE = 15  # seconds; slow providers are skipped, not waited for
MIN_STREAM_SEEDERS = 3
# Largest sensible size per hour of video for each quality preference (GB/h).
# 1080p web releases are ~1.5-4 GB/h, Blu-ray encodes up to ~8, remuxes 15-30.
GB_PER_HOUR_CAP = {None: 6.0, 1080: 8.0, 2160: 25.0, 720: 2.5}

_QUALITY_RANK = {"4K": 4, "1080p": 3, "720p": 2, "SD": 1, "CAM": 0}
_SOURCE_RANK = {
    "REMUX": 5,
    "BluRay": 4,
    "WEB-DL": 3,
    "WEBRip": 2,
    "WEB": 2,
    "HDTV": 1,
    "DVD": 0,
}
_SOURCE_LABEL = {
    "REMUX": "Blu-ray remux",
    "BluRay": "Blu-ray",
    "WEB-DL": "Web",
    "WEBRip": "Web",
    "WEB": "Web",
    "HDTV": "TV",
    "DVD": "DVD",
}


def build_magnet(info_hash: str, name: str) -> str:
    return f"magnet:?xt=urn:btih:{info_hash}&dn={quote(name)}" + "".join(
        f"&tr={quote(t)}" for t in TRACKERS
    )


@dataclass
class SearchQuery:
    title: str  # primary (English) title, used for text queries
    titles: list[str]  # every acceptable title for matching
    media_type: str = "movie"
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    imdb_id: str | None = None  # 'tt1234567'
    runtime: int | None = None  # minutes, for the bitrate estimate
    quality: int | None = (
        None  # preferred resolution: 2160 / 1080 / 720, None = automatic
    )
    season_episodes: dict[int, int] = field(
        default_factory=dict
    )  # season -> episode count
    total_episodes: int | None = None
    free_text: bool = False

    def episodes_in(self, info: ReleaseInfo) -> int | None:
        """How many episodes a torrent contains (1 for a single movie/episode)."""
        pack = pack_kind(info)
        if pack in ("season", "multi"):
            return sum(self.season_episodes.get(n, 0) for n in info.seasons) or None
        if pack == "complete":
            return self.total_episodes
        return max(1, len(info.episodes))

    def gb_per_hour(self, info: ReleaseInfo, size: int) -> float | None:
        """Size per hour of video, the fair way to compare a movie, an episode and a pack."""
        episodes = self.episodes_in(info)
        runtime = self.runtime or (120 if self.media_type == "movie" else 45)
        if not size or not episodes:
            return None
        return size / episodes / (runtime / 60) / 1024**3

    def text_queries(self) -> list[str]:
        if self.free_text:
            return [self.title]
        if self.media_type == "movie":
            return [f"{self.title} {self.year}" if self.year else self.title]
        if self.season is not None and self.episode is not None:
            return [
                f"{self.title} S{self.season:02d}E{self.episode:02d}",
                f"{self.title} S{self.season:02d}",
            ]
        if self.season is not None:
            return [
                f"{self.title} S{self.season:02d}",
                f"{self.title} Season {self.season}",
            ]
        return [self.title]

    def cache_key(self) -> tuple:
        return (
            self.title.lower(),
            self.media_type,
            self.year,
            self.season,
            self.episode,
            self.free_text,
            self.quality,
        )


@dataclass
class TorrentResult:
    raw: RawTorrent
    info: ReleaseInfo
    score: float
    providers: list[str] = field(default_factory=list)
    gb_per_hour: float | None = None
    per_episode: int | None = None  # bytes per episode for packs
    oversized: bool = False  # bigger than the preferred quality needs

    @property
    def quality(self) -> str | None:
        if self.info.is_cam:
            return "CAM"
        resolution = self.info.resolution or 0
        if resolution >= 2160:
            return "4K"
        if resolution >= 1080:
            return "1080p"
        if resolution >= 720:
            return "720p"
        return "SD" if resolution or self.info.source in ("DVD", "HDTV") else None

    @property
    def availability(self) -> str:
        seeders = self.raw.seeders
        if seeders >= 50:
            return "excellent"
        if seeders >= 15:
            return "good"
        if seeders >= MIN_STREAM_SEEDERS:
            return "weak"
        return "none"

    @property
    def low(self) -> bool:
        """Worth hiding when something better exists."""
        return (
            self.info.is_cam
            or self.info.is_junk
            or self.raw.seeders < MIN_STREAM_SEEDERS
            or self.quality == "SD"
        )

    def to_dict(self) -> dict[str, Any]:
        pack = pack_kind(self.info)
        languages = (["CZ"] if self.info.has_czech else []) + (
            ["SK"] if self.info.has_slovak else []
        )
        return {
            "name": self.raw.name,
            "hash": self.raw.info_hash,
            "magnet": self.raw.magnet
            or build_magnet(self.raw.info_hash, self.raw.name),
            "size": format_size(self.raw.size),
            "size_bytes": self.raw.size,
            "seeders": self.raw.seeders,
            "leechers": self.raw.leechers,
            "providers": self.providers,
            "score": self.score,
            "quality": self.quality,
            "source": _SOURCE_LABEL.get(self.info.source or ""),
            "codec": self.info.codec,
            "audio": self.info.audio,
            "browser": self.info.browser_friendly,
            "cam": self.info.is_cam,
            "pack": pack,
            "languages": languages,
            "availability": self.availability,
            "gb_per_hour": round(self.gb_per_hour, 2) if self.gb_per_hour else None,
            "per_episode": format_size(self.per_episode)
            if pack and self.per_episode
            else None,
            "oversized": self.oversized,
            "low": self.low,
        }


def choose_picks(
    ranked: list[TorrentResult], quality: int | None = None
) -> dict[str, str | None]:
    """Pick the recommended / best-quality / smallest versions (by info hash)."""
    playable = [
        r
        for r in ranked
        if not r.info.is_cam
        and not r.info.is_junk
        and r.raw.seeders >= MIN_STREAM_SEEDERS
    ]
    # The "best quality" pick may be large (up to what 4K needs), the default may not.
    quality_pool = [
        r for r in playable if (r.gb_per_hour or 0) <= GB_PER_HOUR_CAP[2160]
    ]
    # Never pick a needlessly huge file by default when a reasonable one exists.
    playable = [r for r in playable if not r.oversized] or playable
    candidates = playable
    if quality:
        # Honour an explicit quality preference whenever such a version is available.
        preferred = [
            r
            for r in playable
            if (r.info.resolution or 0) == quality and r.raw.seeders >= 5
        ]
        candidates = preferred or playable
    # The player is a browser: prefer a version it can decode if one is reasonably available.
    # 4K is the exception: those are nearly always HEVC, so a 4K fan gets 4K even if it needs VLC.
    in_browser = [
        r for r in candidates if r.info.browser_friendly is True and r.raw.seeders >= 5
    ]
    if in_browser and quality != 2160:
        recommended = in_browser[0]
    else:
        recommended = candidates[0] if candidates else (ranked[0] if ranked else None)
    picks: dict[str, str | None] = {
        "recommended": recommended.raw.info_hash if recommended else None,
        "best_quality": None,
        "smallest": None,
    }
    if not recommended or not playable:
        return picks

    healthy = [r for r in playable if r.raw.seeders >= 10]
    quality_candidates = [r for r in quality_pool if r.raw.seeders >= 10]
    if quality_candidates:
        best = max(
            quality_candidates,
            key=lambda r: (
                _QUALITY_RANK.get(r.quality or "", 1),
                _SOURCE_RANK.get(r.info.source or "", 0),
                r.score,
            ),
        )
        if _QUALITY_RANK.get(best.quality or "", 1) > _QUALITY_RANK.get(
            recommended.quality or "", 1
        ):
            picks["best_quality"] = best.raw.info_hash

    single = [
        r
        for r in healthy
        if not pack_kind(r.info)
        and r.raw.size
        and _QUALITY_RANK.get(r.quality or "", 1) >= 2
    ]
    if single and recommended.raw.size:
        small = min(single, key=lambda r: (r.raw.size, -r.score))
        if (
            small.raw.size < 0.6 * recommended.raw.size
            and small.raw.info_hash != picks["best_quality"]
        ):
            picks["smallest"] = small.raw.info_hash
    return picks


def notice_for(ranked: list[TorrentResult]) -> str | None:
    """A plain-language note when the situation needs explaining."""
    if not ranked:
        return None
    if all(
        r.info.is_cam for r in ranked if r.raw.seeders >= MIN_STREAM_SEEDERS
    ) and any(r.info.is_cam for r in ranked):
        return "cam_only"
    if all(r.raw.seeders < MIN_STREAM_SEEDERS for r in ranked):
        return "low_availability"
    return None


class TorrentSearcher:
    """Runs providers in parallel, merges duplicates, ranks and caches results."""

    def __init__(self):
        timeout = config.TORRENT_TIMEOUT
        self.providers: list[Provider] = [
            YTSProvider(config.YTS_MIRRORS, timeout),
            EZTVProvider(config.EZTV_MIRRORS, timeout),
            APIBayProvider(config.APIBAY_MIRRORS, timeout),
            KnabenProvider(config.KNABEN_MIRRORS, timeout),
            TorrentsCSVProvider(config.TORRENTS_CSV_MIRRORS, timeout),
        ]
        self._pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="torrents")
        self._cache: dict[tuple, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def search(self, query: SearchQuery) -> dict[str, Any]:
        key = query.cache_key()
        with self._lock:
            cached = self._cache.get(key)
        if cached and cached[0] > time.monotonic():
            return {**cached[1], "cached": True}

        started = time.monotonic()
        providers = [p for p in self.providers if query.media_type in p.media_types]
        futures = {self._pool.submit(p.search, query): p for p in providers}
        done, pending = wait(futures, timeout=SEARCH_DEADLINE)
        raw: list[RawTorrent] = []
        failed = [futures[f].name for f in pending]
        for future in done:
            provider = futures[future]
            try:
                found = future.result()
                logger.info("%s: %d torrents", provider.name, len(found))
                raw.extend(found)
            except Exception as e:  # one broken index must not break the search
                logger.warning("%s failed: %s", provider.name, e)
                failed.append(provider.name)

        ranked = self._rank(raw, query)
        result = {
            "results": [r.to_dict() for r in ranked[: config.MAX_TORRENT_RESULTS]],
            "picks": choose_picks(ranked, query.quality),
            "notice": notice_for(ranked),
            "total_found": len({r.info_hash for r in raw}),
            "failed_providers": sorted(failed),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        if not failed or ranked:
            with self._lock:
                if len(self._cache) > 500:
                    self._cache.clear()
                self._cache[key] = (time.monotonic() + CACHE_TTL, result)
        return result

    @staticmethod
    def _rank(raw: list[RawTorrent], query: SearchQuery) -> list[TorrentResult]:
        target = MatchTarget(
            titles=query.titles,
            media_type=query.media_type,
            year=query.year,
            season=query.season,
            episode=query.episode,
            free_text=query.free_text,
            preferred_resolution=query.quality,
        )
        merged: dict[str, tuple[RawTorrent, list[str]]] = {}
        for item in raw:
            if item.info_hash in merged:
                existing, providers = merged[item.info_hash]
                existing.seeders = max(existing.seeders, item.seeders)
                existing.leechers = max(existing.leechers, item.leechers)
                existing.id_match = existing.id_match or item.id_match
                existing.magnet = existing.magnet or item.magnet
                if item.provider not in providers:
                    providers.append(item.provider)
                # YTS names are synthesised from structured data, so prefer them for parsing.
                if item.provider == "YTS":
                    existing.name = item.name
            else:
                merged[item.info_hash] = (item, [item.provider])

        ranked = []
        for item, providers in merged.values():
            info = parse_release(item.name)
            score = score_torrent(
                info, item.seeders, item.size, target, id_match=item.id_match
            )
            if score is None:
                continue
            result = TorrentResult(item, info, score, providers)
            result.gb_per_hour = query.gb_per_hour(info, item.size)
            episodes = query.episodes_in(info)
            if pack_kind(info) and episodes and item.size:
                result.per_episode = item.size // episodes
            cap = GB_PER_HOUR_CAP[query.quality]
            if result.gb_per_hour and result.gb_per_hour > cap:
                result.oversized = True
                result.score = round(
                    result.score - (25 if result.gb_per_hour > 2 * cap else 12), 1
                )
            ranked.append(result)
        ranked.sort(key=lambda r: (r.score, r.raw.seeders), reverse=True)
        return ranked

    def provider_status(self) -> list[dict[str, Any]]:
        availability = list(self._pool.map(lambda p: p.is_available(), self.providers))
        return [
            {"name": p.name, "available": ok, "url": p.mirrors[0]}
            for p, ok in zip(self.providers, availability, strict=True)
        ]
