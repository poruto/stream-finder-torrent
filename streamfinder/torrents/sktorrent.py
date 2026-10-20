"""SkTorrent (sktorrent.eu): the big Czech/Slovak tracker - dubbed films and series.

Searching needs a (free) account; the login is entered on the Settings page.
Its torrents are public (DHT works), but each .torrent also carries the account's
personal announce URL, which finds far more Czech peers. That URL is kept on the
server: `extra_trackers()` adds it when TorrServer opens a SkTorrent torrent, so
it never reaches the browser.

Release names look like
  "Perníkový táta / Breaking Bad 3. série (2010)(CZ/EN)[1080p][HEVC] = CSFD 93%"
and are rewritten into the scene style the shared parser understands:
  "Breaking Bad S03 2010 CZ EN dabing 1080p HEVC".
"""

import html
import logging
import re
import threading
import time

import requests

from ..matching import parse_size, title_similarity
from .providers import USER_AGENT, RawTorrent

logger = logging.getLogger(__name__)

BASE_URL = "https://sktorrent.eu/torrent"
SESSION_TTL = 6 * 3600
# Category ids (from the search form): films, HD/UHD films, series, documentaries, TV shows.
MOVIE_CATEGORIES = {1, 3, 5, 14, 15, 17, 19, 20, 28, 29, 31, 43}
TV_CATEGORIES = {16, 17, 42}
SUBTITLED_CATEGORIES = {15}  # "Filmy s titulkama"
PUBLIC_TRACKERS = ["udp://ipv4announce.sktorrent.eu:6969/announce"]

_BLOCK_SPLIT = '<TD align="center" class=lista'
_DETAILS_RE = re.compile(
    r'details\.php\?name=[^"]*?&(?:amp;)?id=([a-f0-9]{40})"[^>]*>.*?<br>([^<]+)</A>',
    re.DOTALL | re.IGNORECASE,
)
_CATEGORY_RE = re.compile(r"category=(\d+)")
_FLAG_RE = re.compile(r"flag/(\w+)\.png")
_SIZE_RE = re.compile(r"Velkost\s+([\d.,]+\s*[KMGT]B)", re.IGNORECASE)
_SEED_RE = re.compile(r"Odosielaju\s*:\s*(\d+)", re.IGNORECASE)
_LEECH_RE = re.compile(r"Stahuju\s*:\s*(\d+)", re.IGNORECASE)
_CSFD_RE = re.compile(r"\s*=\s*CSFD.*$", re.IGNORECASE)
# "3. série", "1.serie", "Série 5", "1-5. serie", "2. řada"
_SEASON_RE = re.compile(
    r"\b(?:(\d{1,2})(?:\s*-\s*(\d{1,2}))?\s*\.\s*(?:s[ée]rie|serie|řada|rada)|"
    r"(?:s[ée]rie|serie|řada|rada)\s+(\d{1,2}))\b",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\((\d{4})(?:\s*-\s*\d{4})?\)")
_LANG_FLAGS = {"cz": "CZ", "sk": "SK", "gb": "EN", "us": "EN"}


def _flatten(text: str) -> str:
    return " ".join(html.unescape(text).split())


def scene_name(
    title: str, flags: list[str], category: int | None, titles: list[str]
) -> str:
    """SkTorrent title -> scene-style release name (see module docstring)."""
    title = _CSFD_RE.sub("", _flatten(title))
    split = re.search(r"[(\[]", title)
    head, tags = (
        (title[: split.start()], title[split.start() :]) if split else (title, "")
    )
    season = ""
    if match := _SEASON_RE.search(head):
        first, last, alone = match.groups()
        if alone:
            season = f"S{int(alone):02d}"
        elif last:
            season = f"S{int(first):02d}-S{int(last):02d}"
        else:
            season = f"S{int(first):02d}"
        head = head[: match.start()] + head[match.end() :]
    # "Czech title / English title": keep the one that matches what we look for.
    names = [n.strip(" -:") for n in head.split(" / ") if n.strip(" -:")] or [head]
    name = (
        max(names, key=lambda n: title_similarity(n, titles)) if titles else names[-1]
    )
    year = _YEAR_RE.search(tags)
    languages = list(dict.fromkeys(_LANG_FLAGS[f] for f in flags if f in _LANG_FLAGS))
    lowered = tags.lower()
    subtitled = category in SUBTITLED_CATEGORIES or "tit" in lowered
    audio = "" if subtitled else ("dabing" if {"CZ", "SK"} & set(languages) else "")
    rest = re.sub(r"\(\d{4}(?:\s*-\s*\d{4})?\)", " ", tags)
    rest = re.sub(r"[()\[\]/,+]", " ", rest)
    parts = [name, season, year.group(1) if year else "", *languages, audio, rest]
    if subtitled and "tit" not in lowered:
        parts.append("CZtitulky")
    return " ".join(" ".join(parts).split())


class SkTorrentProvider:
    """Not a JSON mirror like the others: logs in and reads the search page."""

    name = "SkTorrent"
    media_types = ("movie", "tv")

    def __init__(self, username: str, password: str, timeout: int):
        self.timeout = timeout
        self.mirrors = [BASE_URL]
        self._lock = threading.Lock()
        self._logged_in_at = 0.0
        self._announce: list[str] | None = None
        self._seen: set[str] = set()
        self.configure(username, password)

    def configure(self, username: str, password: str) -> None:
        with self._lock:
            self.username, self.password = username, password
            self.session = requests.Session()
            self.session.headers["User-Agent"] = USER_AGENT
            self._logged_in_at = 0.0
            self._announce = None

    @property
    def enabled(self) -> bool:
        return bool(self.username and self.password)

    # -- session ----------------------------------------------------------------

    def _login(self, force: bool = False) -> None:
        with self._lock:
            if not force and time.monotonic() - self._logged_in_at < SESSION_TTL:
                return
            response = self.session.post(
                f"{BASE_URL}/login.php",
                data={"uid": self.username, "pwd": self.password},
                timeout=self.timeout,
            )
            response.raise_for_status()
            if "uid" not in self.session.cookies or "pass" not in self.session.cookies:
                raise ConnectionError("SkTorrent: login rejected")
            self._logged_in_at = time.monotonic()

    def check_login(self, username: str, password: str) -> bool:
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        session.post(
            f"{BASE_URL}/login.php",
            data={"uid": username, "pwd": password},
            timeout=self.timeout,
        )
        return "uid" in session.cookies and "pass" in session.cookies

    def _page(self, query: str) -> str:
        self._login()
        for attempt in range(2):
            response = self.session.get(
                f"{BASE_URL}/torrents_v2.php",
                params={"search": query, "category": 0, "active": 0},
                timeout=self.timeout,
            )
            response.raise_for_status()
            page = _decode(response.content)
            if 'name="pwd"' not in page or attempt:  # still logged in
                return page
            self._login(force=True)
        return ""

    # -- search -----------------------------------------------------------------

    def search(self, query) -> list[RawTorrent]:
        if not self.enabled:
            return []
        terms = (
            [query.title] if query.free_text else [query.title, *query.local_titles[:1]]
        )
        allowed = MOVIE_CATEGORIES if query.media_type == "movie" else TV_CATEGORIES
        results: dict[str, RawTorrent] = {}
        for term in dict.fromkeys(terms):
            try:
                page = self._page(term)
            except requests.RequestException as e:
                raise ConnectionError(f"SkTorrent: {e}") from None
            for item in self.parse(
                page, query.titles, None if query.free_text else allowed
            ):
                results.setdefault(item.info_hash, item)
        self._seen.update(results)
        return list(results.values())

    def parse(
        self, page: str, titles: list[str], categories: set[int] | None
    ) -> list[RawTorrent]:
        found = []
        for block in page.split(_BLOCK_SPLIT)[1:]:
            match = _DETAILS_RE.search(block)
            if not match:
                continue
            category = _CATEGORY_RE.search(block)
            category_id = int(category.group(1)) if category else None
            if categories is not None and category_id not in categories:
                continue
            info_hash, title = match.group(1).lower(), match.group(2)
            size, seeders, leechers = (
                _SIZE_RE.search(block),
                _SEED_RE.search(block),
                _LEECH_RE.search(block),
            )
            name = scene_name(title, _FLAG_RE.findall(block), category_id, titles)
            found.append(
                RawTorrent(
                    name=name,
                    info_hash=info_hash,
                    size=parse_size(size.group(1)) if size else 0,
                    seeders=int(seeders.group(1)) if seeders else 0,
                    leechers=int(leechers.group(1)) if leechers else 0,
                    provider=self.name,
                    title=_CSFD_RE.sub("", _flatten(title)),
                )
            )
        return found

    def is_available(self) -> bool:
        if not self.enabled:
            return False
        try:
            self._login()
            return True
        except (requests.RequestException, ConnectionError):
            return False

    # -- personal announce URL -----------------------------------------------------

    def extra_trackers(self, info_hash: str) -> list[str]:
        """Trackers to add when this torrent is opened (only for SkTorrent results)."""
        info_hash = info_hash.lower()
        if not self.enabled or info_hash not in self._seen:
            return []
        if self._announce is None:
            try:
                self._announce = self._fetch_announce(info_hash)
            except (requests.RequestException, ConnectionError, ValueError) as e:
                logger.info("SkTorrent announce lookup failed: %s", e)
                return list(PUBLIC_TRACKERS)
        return self._announce + PUBLIC_TRACKERS

    def _fetch_announce(self, info_hash: str) -> list[str]:
        self._login()
        response = self.session.get(
            f"{BASE_URL}/download.php", params={"id": info_hash}, timeout=self.timeout
        )
        response.raise_for_status()
        # Bencoded strings are "<length>:<bytes>": read exactly <length> bytes of each URL.
        data, urls = response.content, []
        for match in re.finditer(rb"(\d+):https?://", data):
            start = match.start() + len(match.group(1)) + 1
            url = data[start : start + int(match.group(1))].decode("latin-1")
            if "announce.php?pid=" in url:
                urls.append(url)
        if not urls:
            raise ValueError("no personal announce URL in the .torrent")
        return list(dict.fromkeys(urls))


def _decode(content: bytes) -> str:
    """The site mixes UTF-8 pages with windows-1250 ones."""
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("cp1250", "replace")
