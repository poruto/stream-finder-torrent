"""TorrServer (MatriX) API client.

Endpoints used (all verified against TorrServer MatriX.136):
  POST /torrents {"action": "add" | "get" | "list" | "rem", ...}
  GET  /stream/<name>?link=<hash>&index=<file id>&play   (supports HTTP Range)
  GET  /echo
"""

import base64
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import requests

from .matching import is_subtitle_path, is_video_path, parse_release

logger = logging.getLogger(__name__)

_BTIH_RE = re.compile(r"urn:btih:([a-zA-Z0-9]{32,40})", re.IGNORECASE)
_HEX40_RE = re.compile(r"^[a-fA-F0-9]{40}$")
_BASE32_RE = re.compile(r"^[A-Za-z2-7]{32}$")


class TorrServerError(Exception):
    """Raised when TorrServer is unreachable or returns an error.

    `code` is one of: offline, timeout, error, no_video (translated by the API).
    """

    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.code = code


def extract_info_hash(magnet_or_hash: str) -> str | None:
    """Return the 40-char lowercase hex info hash from a magnet link or bare hash.

    Handles both hex (40 chars) and base32 (32 chars) btih encodings.
    """
    text = (magnet_or_hash or "").strip()
    match = _BTIH_RE.search(text)
    candidate = match.group(1) if match else text
    if _HEX40_RE.match(candidate):
        return candidate.lower()
    if _BASE32_RE.match(candidate):
        try:
            return base64.b32decode(candidate.upper()).hex()
        except (ValueError, base64.binascii.Error):
            return None
    return None


def normalize_magnet(magnet_or_hash: str) -> str | None:
    """Accept a magnet link or a bare info hash; return a magnet link."""
    text = (magnet_or_hash or "").strip()
    if text.lower().startswith("magnet:?"):
        return text if extract_info_hash(text) else None
    info_hash = extract_info_hash(text)
    return f"magnet:?xt=urn:btih:{info_hash}" if info_hash else None


@dataclass
class TorrentFile:
    index: int
    path: str
    size: int

    @property
    def name(self) -> str:
        return self.path.replace("\\", "/").split("/")[-1]

    @property
    def is_video(self) -> bool:
        return is_video_path(self.path)

    @property
    def is_subtitle(self) -> bool:
        return is_subtitle_path(self.path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "path": self.path,
            "name": self.name,
            "size": self.size,
        }


def _episode_of(file: TorrentFile):
    """(season, episodes) for a file, using parent folders when the name lacks the season."""
    info = parse_release(file.name)
    season = info.season
    if season is None:
        for part in reversed(file.path.replace("\\", "/").split("/")[:-1]):
            folder = parse_release(part)
            if folder.season is not None:
                season = folder.season
                break
    return season, info.episodes


def video_files(files: list[TorrentFile]) -> list[TorrentFile]:
    """Playable files, without samples/extras, sorted naturally by path."""
    videos = [f for f in files if f.is_video and "sample" not in f.path.lower()]
    if len(videos) > 1:
        biggest = max(f.size for f in videos)
        # Drop trailers/featurettes in movie torrents (tiny compared to the main file).
        videos = [f for f in videos if f.size >= biggest * 0.05 or _episode_of(f)[1]]
    return sorted(
        videos,
        key=lambda f: [
            int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", f.path)
        ],
    )


def select_video_file(
    files: list[TorrentFile],
    season: int | None = None,
    episode: int | None = None,
) -> TorrentFile | None:
    """Pick the file to play: the requested episode if given, else the main (largest) video."""
    videos = video_files(files)
    if not videos:
        return None
    if episode is not None:
        matches = []
        for file in videos:
            file_season, file_episodes = _episode_of(file)
            if episode in file_episodes and (
                season is None or file_season in (None, season)
            ):
                matches.append(file)
        if matches:
            return max(matches, key=lambda f: f.size)
    return max(videos, key=lambda f: f.size)


class TorrServerClient:
    """Thin client for the TorrServer HTTP API."""

    def __init__(self, base_url: str, stream_path: str = "/stream", timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.stream_path = "/" + stream_path.strip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _post(self, payload: dict[str, Any], timeout: int | None = None) -> Any:
        try:
            response = self.session.post(
                f"{self.base_url}/torrents",
                json=payload,
                timeout=timeout or self.timeout,
            )
        except requests.ConnectionError as e:
            raise TorrServerError(
                f"TorrServer offline ({self.base_url})", "offline"
            ) from e
        except requests.Timeout as e:
            raise TorrServerError("TorrServer timeout", "timeout") from e
        if response.status_code != 200:
            raise TorrServerError(
                f"TorrServer error {response.status_code}: {response.text[:200]}"
            )
        return response.json() if response.content else None

    def is_online(self) -> bool:
        try:
            return (
                self.session.get(f"{self.base_url}/echo", timeout=3).status_code == 200
            )
        except requests.RequestException:
            return False

    def add(self, magnet: str, title: str = "", poster: str = "") -> str:
        """Add (or re-open) a torrent; returns its info hash."""
        data = self._post(
            {
                "action": "add",
                "link": magnet,
                "title": title,
                "poster": poster,
                "save_to_db": True,
            },
            timeout=30,
        )
        info_hash = (data or {}).get("hash") or extract_info_hash(magnet)
        if not info_hash:
            raise TorrServerError("TorrServer returned no info hash")
        return info_hash.lower()

    def get(self, info_hash: str) -> dict[str, Any] | None:
        """Torrent status, or None when TorrServer has unloaded it (404)."""
        try:
            return self._post({"action": "get", "hash": info_hash}) or {}
        except TorrServerError as e:
            if "404" in str(e):
                return None
            raise

    def files(self, info_hash: str, wait: float = 0) -> list[TorrentFile]:
        """File list of a torrent, polling up to `wait` seconds for metadata to arrive.

        Torrents idle longer than TorrentDisconnectTimeout are unloaded by TorrServer,
        so a missing torrent is re-added from its hash.
        """
        deadline = time.monotonic() + wait
        while True:
            status = self.get(info_hash)
            if status is None:
                self.add(f"magnet:?xt=urn:btih:{info_hash}")
                status = self.get(info_hash) or {}
            stats = status.get("file_stats") or []
            if stats or time.monotonic() >= deadline:
                return [
                    TorrentFile(
                        int(f["id"]), f.get("path", ""), int(f.get("length", 0))
                    )
                    for f in stats
                ]
            time.sleep(1)

    def stream_url(
        self, info_hash: str, file: TorrentFile, base_url: str | None = None
    ) -> str:
        """Stream URL; `base_url` overrides the host for URLs handed to the browser."""
        return (
            f"{(base_url or self.base_url).rstrip('/')}{self.stream_path}/{quote(file.name)}"
            f"?link={info_hash}&index={file.index}&play"
        )

    def read_range(
        self,
        info_hash: str,
        file: TorrentFile,
        start: int,
        length: int,
        timeout: int = 30,
    ) -> bytes:
        """Read `length` bytes at `start` from a file inside the torrent."""
        end = min(start + length, file.size) - 1
        response = self.session.get(
            self.stream_url(info_hash, file),
            headers={"Range": f"bytes={start}-{end}"},
            timeout=timeout,
            stream=True,
        )
        try:
            response.raise_for_status()
            data = b""
            for chunk in response.iter_content(65536):
                data += chunk
                if len(data) >= end - start + 1:
                    break
            return data[: end - start + 1]
        finally:
            response.close()

    def read_file(
        self, info_hash: str, file: TorrentFile, max_bytes: int = 5 * 1024 * 1024
    ) -> bytes:
        if file.size > max_bytes:
            raise TorrServerError(f"{file.name} is too large")
        return self.read_range(info_hash, file, 0, file.size)
