"""Torrent search module with support for multiple providers."""

import re
import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from urllib.parse import quote
from enum import Enum

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import TORRENT_TIMEOUT, MAX_TORRENT_RESULTS


# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class MediaType(Enum):
    """Media type enumeration."""
    MOVIE = "movie"
    TV = "tv"


@dataclass
class TorrentResult:
    """Represents a torrent search result."""
    name: str
    magnet: str
    size: str
    seeders: int
    leechers: int
    source: str
    hash: str = ""
    quality: str = "Unknown"
    category: str = ""
    upload_date: str = ""

    @property
    def ratio(self) -> float:
        """Calculate seed/leech ratio."""
        return self.seeders / max(self.leechers, 1)

    @property
    def health_score(self) -> int:
        """Calculate torrent health score (0-100)."""
        if self.seeders == 0:
            return 0
        if self.seeders >= 50:
            return 100
        return min(100, self.seeders * 2)


@dataclass
class SearchQuery:
    """Represents a torrent search query."""
    title: str
    year: str = ""
    media_type: MediaType = MediaType.MOVIE
    season: Optional[int] = None
    episode: Optional[int] = None
    quality_filter: Optional[str] = None

    def __post_init__(self):
        if isinstance(self.media_type, str):
            self.media_type = MediaType(self.media_type)

    @property
    def formatted_query(self) -> str:
        """Generate formatted search query."""
        query_parts = [self.title]

        if self.year:
            query_parts.append(self.year)

        if self.season and self.episode:
            query_parts.append(f"S{self.season:02d}E{self.episode:02d}")
        elif self.season:
            query_parts.append(f"Season {self.season}")

        return " ".join(query_parts)


class TorrentProviderError(Exception):
    """Base exception for torrent provider errors."""
    pass


class TorrentProvider(ABC):
    """Abstract base class for torrent providers."""

    def __init__(self, name: str, base_url: str, timeout: int = TORRENT_TIMEOUT):
        self.name = name
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self._setup_session()

    def _setup_session(self) -> None:
        """Setup HTTP session with retries and timeouts."""
        self.session = requests.Session()

        retry_strategy = Retry(
            total=2,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )

        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })

    @abstractmethod
    def search(self, query: SearchQuery) -> List[TorrentResult]:
        """Search for torrents."""
        pass

    @abstractmethod
    def is_available(self) -> bool:
        """Check if provider is available."""
        pass

    def _extract_quality(self, name: str) -> str:
        """Extract video quality from torrent name."""
        name_lower = name.lower()

        quality_map = {
            '2160p': '4K',
            '4k': '4K',
            '1080p': '1080p',
            '720p': '720p',
            '480p': 'SD',
            'dvdrip': 'SD',
            'webrip': 'WEB',
            'webdl': 'WEB-DL',
            'bluray': 'BluRay',
            'hdtv': 'HDTV'
        }

        for pattern, quality in quality_map.items():
            if pattern in name_lower:
                return quality

        return "Unknown"

    def _extract_hash(self, magnet: str) -> str:
        """Extract hash from magnet link."""
        match = re.search(r'urn:btih:([a-fA-F0-9]{40}|[a-fA-F0-9]{32})', magnet)
        return match.group(1) if match else ""


class YTSProvider(TorrentProvider):
    """YTS movie provider."""

    def __init__(self):
        super().__init__("YTS", "https://yts.mx")
        self.api_url = f"{self.base_url}/api/v2/list_movies.json"

    def search(self, query: SearchQuery) -> List[TorrentResult]:
        """Search YTS for movies."""
        if query.media_type != MediaType.MOVIE:
            return []

        try:
            params = {
                "query_term": query.formatted_query,
                "limit": min(MAX_TORRENT_RESULTS, 20),
                "sort_by": "seeds",
                "order_by": "desc"
            }

            logger.info(f"🎬 Searching YTS: {query.formatted_query}")

            response = self.session.get(
                self.api_url,
                params=params,
                timeout=self.timeout
            )
            response.raise_for_status()

            data = response.json()

            if not (data.get("status") == "ok" and data.get("data", {}).get("movies")):
                return []

            results = []
            for movie in data["data"]["movies"][:10]:
                for torrent in movie.get("torrents", []):
                    if torrent.get("hash"):
                        result = TorrentResult(
                            name=f"{movie['title']} ({movie['year']}) {torrent.get('quality', '')} [YTS]",
                            magnet=self._build_yts_magnet(torrent["hash"], movie['title']),
                            size=torrent.get("size", "Unknown"),
                            seeders=torrent.get("seeds", 0),
                            leechers=torrent.get("peers", 0),
                            source="YTS",
                            hash=torrent["hash"],
                            quality=torrent.get("quality", "Unknown"),
                            category="Movies"
                        )
                        results.append(result)

            logger.info(f"✅ YTS: Found {len(results)} torrents")
            return results

        except Exception as e:
            logger.error(f"❌ YTS search failed: {e}")
            raise TorrentProviderError(f"YTS search failed: {e}")

    def _build_yts_magnet(self, hash_str: str, title: str) -> str:
        """Build magnet link for YTS torrent."""
        trackers = [
            "udp://tracker.openbittorrent.com:80",
            "udp://open.demonii.com:1337",
            "udp://tracker.coppersurfer.tk:6969",
            "udp://exodus.desync.com:6969"
        ]

        magnet = f"magnet:?xt=urn:btih:{hash_str}&dn={quote(title)}"
        for tracker in trackers:
            magnet += f"&tr={quote(tracker)}"

        return magnet

    def is_available(self) -> bool:
        """Check if YTS is available."""
        try:
            response = self.session.get(f"{self.base_url}/api/v2/list_movies.json",
                                      params={"limit": 1}, timeout=5)
            return response.status_code == 200
        except:
            return False


class TPBProvider(TorrentProvider):
    """The Pirate Bay provider."""

    def __init__(self):
        super().__init__("TPB", "https://thepiratebay.org")
        self.mirrors = [
            "https://thepiratebay.org",
            "https://tpb.party",
            "https://thepiratebay10.org",
            "https://piratebay.live",
            "https://thepiratebay.zone"
        ]
        self._working_mirror = None

    def search(self, query: SearchQuery) -> List[TorrentResult]:
        """Search TPB for content."""
        for mirror in self.mirrors:
            try:
                logger.info(f"🌐 Trying TPB mirror: {mirror}")
                results = self._search_mirror(mirror, query)

                if results:
                    self._working_mirror = mirror
                    logger.info(f"✅ {mirror}: Found {len(results)} torrents")
                    return results

                time.sleep(0.3)  # Rate limiting

            except Exception as e:
                logger.warning(f"❌ {mirror}: {str(e)[:50]}")
                continue

        logger.error("❌ All TPB mirrors failed")
        return []

    def _search_mirror(self, mirror: str, query: SearchQuery) -> List[TorrentResult]:
        """Search specific TPB mirror."""
        # Determine category
        category = "200" if query.media_type == MediaType.TV else "201"  # TV/Movies

        search_url = f"{mirror}/search/{quote(query.formatted_query)}/1/99/{category}"

        response = self.session.get(search_url, timeout=self.timeout)
        response.raise_for_status()

        return self._parse_tpb_html(response.text, mirror)

    def _parse_tpb_html(self, html: str, base_url: str) -> List[TorrentResult]:
        """Parse TPB HTML response."""
        results = []

        # Find searchResult table
        table_match = re.search(
            r'<table[^>]*id="searchResult"[^>]*>(.*?)</table>',
            html,
            re.DOTALL
        )

        if not table_match:
            return []

        # Parse rows
        row_pattern = r'<tr[^>]*>(.*?)</tr>'
        rows = re.findall(row_pattern, table_match.group(1), re.DOTALL)

        for row in rows[:MAX_TORRENT_RESULTS]:
            result = self._parse_tpb_row(row, base_url)
            if result:
                results.append(result)

        return results

    def _parse_tpb_row(self, row_html: str, base_url: str) -> Optional[TorrentResult]:
        """Parse single TPB table row."""
        try:
            # Extract torrent name
            name_match = re.search(
                r'<a[^>]*class="detLink"[^>]*title="[^"]*Details for ([^"]*)"[^>]*>',
                row_html
            )

            if not name_match:
                return None

            name = name_match.group(1).strip()

            # Extract magnet link
            magnet_match = re.search(
                r'<a[^>]*href="(magnet:\?xt=urn:btih:[^"]*)"[^>]*>',
                row_html
            )

            if not magnet_match:
                return None

            magnet = magnet_match.group(1)

            # Extract seeders and leechers
            seed_leech_matches = re.findall(r'<td[^>]*align="right">(\d+)</td>', row_html)
            seeders = int(seed_leech_matches[0]) if seed_leech_matches else 0
            leechers = int(seed_leech_matches[1]) if len(seed_leech_matches) > 1 else 0

            # Extract size
            size_match = re.search(r'Size ([^,]+),', row_html)
            size = size_match.group(1).strip() if size_match else "Unknown"

            return TorrentResult(
                name=f"{name} [TPB]",
                magnet=magnet,
                size=size,
                seeders=seeders,
                leechers=leechers,
                source="TPB",
                hash=self._extract_hash(magnet),
                quality=self._extract_quality(name),
                category="Mixed"
            )

        except Exception as e:
            logger.warning(f"❌ TPB row parsing failed: {e}")
            return None

    def is_available(self) -> bool:
        """Check if any TPB mirror is available."""
        for mirror in self.mirrors:
            try:
                response = self.session.get(f"{mirror}/", timeout=5)
                if response.status_code == 200:
                    return True
            except:
                continue
        return False


class TorrentSearcher:
    """Main torrent searcher with multiple providers."""

    def __init__(self):
        self.providers = {
            'yts': YTSProvider(),
            'tpb': TPBProvider()
        }

        # Provider priority for different media types
        self.provider_priority = {
            MediaType.MOVIE: ['yts', 'tpb'],
            MediaType.TV: ['tpb']
        }

    def search_torrents(self, title: str, year: str = "", media_type: str = "movie",
                       season: Optional[int] = None, episode: Optional[int] = None,
                       quality_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Search for torrents across providers.

        Args:
            title: Content title
            year: Release year
            media_type: 'movie' or 'tv'
            season: Season number (for TV shows)
            episode: Episode number (for TV shows)
            quality_filter: Quality filter (e.g., '1080p')

        Returns:
            List of torrent results as dictionaries
        """
        query = SearchQuery(
            title=title,
            year=year,
            media_type=MediaType(media_type),
            season=season,
            episode=episode,
            quality_filter=quality_filter
        )

        logger.info(f"🔍 Searching for: {query.formatted_query} ({media_type})")

        all_results = []
        providers_to_try = self.provider_priority.get(query.media_type, ['tpb'])

        for provider_name in providers_to_try:
            provider = self.providers.get(provider_name)
            if not provider:
                continue

            try:
                results = provider.search(query)
                all_results.extend(results)

                # For movies, if YTS returns results, use only those
                if query.media_type == MediaType.MOVIE and provider_name == 'yts' and results:
                    logger.info("🎬 Using YTS results for movie")
                    break

            except TorrentProviderError as e:
                logger.warning(f"Provider {provider_name} failed: {e}")
                continue

        # Sort results by health score and seeders
        all_results.sort(key=lambda x: (x.health_score, x.seeders), reverse=True)

        # Convert to dictionaries for API compatibility
        return [self._torrent_to_dict(result) for result in all_results[:MAX_TORRENT_RESULTS]]

    def _torrent_to_dict(self, torrent: TorrentResult) -> Dict[str, Any]:
        """Convert TorrentResult to dictionary."""
        return {
            "name": torrent.name,
            "magnet": torrent.magnet,
            "size": torrent.size,
            "seeders": torrent.seeders,
            "leechers": torrent.leechers,
            "source": torrent.source,
            "hash": torrent.hash,
            "quality": torrent.quality,
            "health_score": torrent.health_score,
            "ratio": round(torrent.ratio, 2)
        }

    def get_tracker_status(self) -> Dict[str, Any]:
        """Get status of all torrent providers."""
        status = {}

        for name, provider in self.providers.items():
            try:
                is_available = provider.is_available()
                status[name.upper()] = {
                    "available": is_available,
                    "name": provider.name,
                    "url": provider.base_url
                }
            except Exception as e:
                status[name.upper()] = {
                    "available": False,
                    "name": provider.name,
                    "error": str(e)
                }

        return status

    def add_provider(self, name: str, provider: TorrentProvider) -> None:
        """Add custom torrent provider."""
        self.providers[name] = provider

    def remove_provider(self, name: str) -> None:
        """Remove torrent provider."""
        if name in self.providers:
            del self.providers[name]


# Factory functions for easy provider creation
def create_yts_provider() -> YTSProvider:
    """Create YTS provider instance."""
    return YTSProvider()


def create_tpb_provider() -> TPBProvider:
    """Create TPB provider instance."""
    return TPBProvider()


# Utility functions
def validate_magnet_link(magnet: str) -> bool:
    """Validate magnet link format."""
    magnet_pattern = r'^magnet:\?xt=urn:btih:[a-fA-F0-9]{40}.*'
    return bool(re.match(magnet_pattern, magnet))


def extract_torrent_info(magnet: str) -> Dict[str, str]:
    """Extract information from magnet link."""
    info = {}

    # Extract hash
    hash_match = re.search(r'urn:btih:([a-fA-F0-9]{40})', magnet)
    if hash_match:
        info['hash'] = hash_match.group(1)

    # Extract display name
    dn_match = re.search(r'dn=([^&]+)', magnet)
    if dn_match:
        info['name'] = dn_match.group(1).replace('+', ' ')

    # Extract trackers
    trackers = re.findall(r'tr=([^&]+)', magnet)
    info['trackers'] = trackers

    return info