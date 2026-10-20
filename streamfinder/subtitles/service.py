"""Automatic subtitle finder.

Sources, searched in parallel for the file that is playing:
  1. Subtitles bundled inside the torrent (same release, so always in sync).
  2. OpenSubtitles.com API (needs a free API key): searched by TMDB id and the
     OpenSubtitles moviehash of the actual file, which flags exact-file matches.
  3. OpenSubtitles.org legacy site: same archive, no API key, no 5-a-day quota.

Every .com result that also exists on .org (`legacy_subtitle_id`) is downloaded
through .org, so the API download quota is only used as a last resort.
Candidates are ranked by hash match, release-name similarity (group / source /
service / edition), popularity and quality flags.
"""

import hashlib
import logging
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

import requests

from .. import config
from ..matching import ReleaseInfo, parse_release, release_match_score
from ..torrserver import (
    TorrentFile,
    TorrServerClient,
    TorrServerError,
    select_video_file,
)
from .formats import (
    HASH_CHUNK,
    LANGUAGE_NAMES,
    _vtt_time,
    decode_subtitle,
    language_from_filename,
    opensubtitles_hash,
    parse_cues,
    to_webvtt,
)
from .opensubtitles import OpenSubtitlesClient
from .opensubtitles_org import OpenSubtitlesOrgClient

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"os-\d+|org-\d+|tor-[a-f0-9]{40}-\d+")


@dataclass
class SubtitleCandidate:
    id: str  # 'org-<IDSubtitle>' | 'os-<file_id>' | 'tor-<hash>-<index>'
    language: str
    source: str  # 'torrent' | 'opensubtitles'
    release: str
    score: float = 0.0
    hash_match: bool = False
    hearing_impaired: bool = False
    machine_translated: bool = False
    downloads: int = 0
    fps: float | None = None
    file_id: int | None = (
        None  # OpenSubtitles.com file: downloads 'org-' ones when .org refuses
    )

    @property
    def language_name(self) -> str:
        return LANGUAGE_NAMES.get(self.language, self.language)

    @property
    def note(self) -> str:
        """Short hint code shown next to the subtitle (translated in the browser)."""
        if self.source == "torrent":
            return "torrent"
        if self.hash_match:
            return "hash"
        if self.machine_translated:
            return "machine"
        if self.hearing_impaired:
            return "hi"
        return ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.update(
            language_name=self.language_name,
            note=self.note,
            url=f"/api/subtitles/{self.id}.vtt",
        )
        return data


def _subtitle_belongs_to(
    sub: TorrentFile, video: TorrentFile, video_count: int
) -> bool:
    """Is a subtitle file inside the torrent meant for this video file?"""
    if video_count <= 1:
        return True
    video_stem = os.path.splitext(video.name)[0].lower()
    if video_stem and video_stem in sub.path.lower():
        return True
    video_info, sub_info = parse_release(video.name), parse_release(sub.path)
    return (
        bool(video_info.episodes)
        and video_info.episodes == sub_info.episodes
        and sub_info.season in (None, video_info.season)
    )


def _popularity(downloads: int) -> float:
    return min(10.0, 2.5 * math.log10(downloads + 1))


class SubtitleService:
    """Finds, ranks, downloads and converts subtitles for a playing torrent."""

    def __init__(self, torrserver: TorrServerClient, cache_dir: str | None = None):
        self.torrserver = torrserver
        self.languages = config.SUBTITLE_LANGUAGES
        self.api = OpenSubtitlesClient(
            config.OPENSUBTITLES_API_KEY,
            config.OPENSUBTITLES_USERNAME,
            config.OPENSUBTITLES_PASSWORD,
            config.OPENSUBTITLES_USER_AGENT,
        )
        self.org = OpenSubtitlesOrgClient()
        self.cache_dir = cache_dir or config.SUBTITLE_CACHE_DIR
        os.makedirs(self.cache_dir, exist_ok=True)
        self._meta: dict[
            str, SubtitleCandidate
        ] = {}  # candidate id -> metadata needed to fetch it
        self._hashes: dict[str, str] = {}  # '<hash>:<index>' -> moviehash
        self._pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="subtitles")
        # OpenSubtitles.org puts downloads behind a bot check at times; then only
        # subtitles the .com API can deliver are offered, for an hour.
        self._org_blocked_until = 0.0

    @property
    def org_blocked(self) -> bool:
        return time.monotonic() < self._org_blocked_until

    def status(self) -> dict[str, Any]:
        return {
            "opensubtitles_api": self.api.enabled,
            "languages": self.languages,
            "language_names": {
                code: LANGUAGE_NAMES.get(code, code) for code in self.languages
            },
        }

    # -- search -----------------------------------------------------------------

    def find(
        self,
        info_hash: str,
        file_index: int | None = None,
        tmdb_id: int | None = None,
        media_type: str = "movie",
        season: int | None = None,
        episode: int | None = None,
        imdb_id: str | None = None,
        episode_imdb_id: str | None = None,
        title: str = "",
        year: int | None = None,
        languages: list[str] | None = None,
    ) -> dict[str, Any]:
        languages = [lang for lang in (languages or self.languages) if lang] or ["en"]
        files = self.torrserver.files(info_hash, wait=10)
        video = next(
            (f for f in files if f.index == file_index), None
        ) or select_video_file(files, season, episode)
        if video is None:
            raise TorrServerError("No video in torrent", "no_video")
        video_info = parse_release(video.path)
        if media_type == "tv" and video_info.episodes:
            # In a season pack the playing file decides which episode we need subtitles for.
            season, episode = video_info.season or season, video_info.episodes[0]

        candidates = self._bundled(info_hash, files, video, languages)
        warnings = []

        org_future = None
        org_imdb = episode_imdb_id if media_type == "tv" else imdb_id
        if org_imdb:
            org_future = self._pool.submit(self.org.search, org_imdb, languages)
        api_items: list[dict[str, Any]] = []
        if self.api.enabled:
            try:
                api_items = self._search_api(
                    info_hash,
                    video,
                    tmdb_id,
                    media_type,
                    season,
                    episode,
                    imdb_id,
                    title,
                    year,
                    languages,
                )
            except requests.RequestException as e:
                logger.warning("OpenSubtitles API search failed: %s", e)
        org_items = []
        if org_future:
            try:
                org_items = org_future.result(timeout=20)
            except Exception as e:  # network, parse or timeout errors
                logger.warning("OpenSubtitles.org search failed: %s", e)
        if not api_items and not org_items and not candidates:
            warnings.append("no_online")

        online = self._merge_online(api_items, org_items, video_info, languages)
        if self.org_blocked:
            online = [c for c in online if c.id.startswith("os-") or c.file_id]
        candidates += online
        for candidate in candidates:
            self._meta[candidate.id] = candidate
        candidates.sort(
            key=lambda c: (
                -c.score,
                languages.index(c.language) if c.language in languages else 99,
            )
        )

        best = next(
            (c for lang in languages for c in candidates if c.language == lang), None
        )
        return {
            "video": video.to_dict(),
            "candidates": [c.to_dict() for c in candidates[:40]],
            "best": best.to_dict() if best else None,
            "warnings": warnings,
        }

    def _bundled(
        self,
        info_hash: str,
        files: list[TorrentFile],
        video: TorrentFile,
        languages: list[str],
    ) -> list[SubtitleCandidate]:
        video_count = sum(1 for f in files if f.is_video)
        has_idx = any(f.name.lower().endswith(".idx") for f in files)
        result = []
        for file in files:
            if not file.is_subtitle or (has_idx and file.name.lower().endswith(".sub")):
                continue  # VobSub .idx/.sub are images, not text
            if not _subtitle_belongs_to(file, video, video_count):
                continue
            language = language_from_filename(file.path) or (
                "en" if "en" in languages else languages[0]
            )
            if language not in languages:
                continue
            lowered = file.path.lower()
            result.append(
                SubtitleCandidate(
                    id=f"tor-{info_hash}-{file.index}",
                    language=language,
                    source="torrent",
                    release=file.name,
                    score=200 - (10 if "forced" in lowered else 0),
                    hearing_impaired=any(t in lowered for t in ("sdh", ".hi.", "_hi")),
                )
            )
        return result

    def _moviehash(self, info_hash: str, video: TorrentFile) -> str | None:
        key = f"{info_hash}:{video.index}"
        if key not in self._hashes:
            try:
                head = self._pool.submit(
                    self.torrserver.read_range, info_hash, video, 0, HASH_CHUNK, 25
                )
                tail = self._pool.submit(
                    self.torrserver.read_range,
                    info_hash,
                    video,
                    video.size - HASH_CHUNK,
                    HASH_CHUNK,
                    25,
                )
                self._hashes[key] = opensubtitles_hash(
                    video.size, head.result(), tail.result()
                )
            except (requests.RequestException, ValueError) as e:
                logger.info("moviehash unavailable for %s: %s", video.name, e)
                return None
        return self._hashes[key]

    def _search_api(
        self,
        info_hash,
        video,
        tmdb_id,
        media_type,
        season,
        episode,
        imdb_id,
        title,
        year,
        languages,
    ):
        base = {
            "languages": ",".join(sorted(languages)),
            "moviehash": self._moviehash(info_hash, video),
        }
        if media_type == "tv":
            base.update(type="episode", season_number=season, episode_number=episode)
            by_id = {"parent_tmdb_id": tmdb_id} if tmdb_id else {}
            if not by_id and imdb_id:
                by_id = {"parent_imdb_id": int(imdb_id.removeprefix("tt"))}
        else:
            base["type"] = "movie"
            by_id = {"tmdb_id": tmdb_id} if tmdb_id else {}
            if not by_id and imdb_id:
                by_id = {"imdb_id": int(imdb_id.removeprefix("tt"))}
        items = self.api.search({**base, **by_id}) if by_id else []
        if not items and title:
            items = self.api.search(
                {
                    **base,
                    "query": title,
                    "year": year if media_type == "movie" else None,
                }
            )
        return items

    @staticmethod
    def _merge_online(
        api_items, org_items, video_info: ReleaseInfo, languages: list[str]
    ) -> list[SubtitleCandidate]:
        """One candidate per subtitle; prefer the quota-free .org download."""
        merged: dict[str, SubtitleCandidate] = {}
        for item in api_items:
            attrs = item.get("attributes", {})
            files = attrs.get("files") or []
            language = (attrs.get("language") or "").lower().split("-")[0]
            if not files or language not in languages:
                continue
            legacy_id = attrs.get("legacy_subtitle_id")
            hash_match = bool(attrs.get("moviehash_match"))
            machine = bool(
                attrs.get("machine_translated") or attrs.get("ai_translated")
            )
            release = attrs.get("release") or files[0].get("file_name") or ""
            score = release_match_score(release, video_info) + _popularity(
                attrs.get("download_count") or 0
            )
            score += (100 if hash_match else 0) + (
                3 if attrs.get("from_trusted") else 0
            )
            score -= (
                (3 if attrs.get("hearing_impaired") else 0)
                + (25 if machine else 0)
                + 5 * max(0, len(files) - 1)
            )
            candidate_id = (
                f"org-{legacy_id}" if legacy_id else f"os-{files[0]['file_id']}"
            )
            merged[candidate_id] = SubtitleCandidate(
                id=candidate_id,
                language=language,
                source="opensubtitles",
                release=release,
                score=round(score, 1),
                hash_match=hash_match,
                hearing_impaired=bool(attrs.get("hearing_impaired")),
                machine_translated=machine,
                downloads=attrs.get("download_count") or 0,
                fps=attrs.get("fps") or None,
                file_id=files[0].get("file_id"),
            )
        for sub in org_items:
            candidate_id = f"org-{sub.id_subtitle}"
            if candidate_id in merged or sub.language not in languages:
                continue
            score = (
                release_match_score(sub.release, video_info)
                + _popularity(sub.downloads)
                - 5 * max(0, sub.cds - 1)
            )
            merged[candidate_id] = SubtitleCandidate(
                id=candidate_id,
                language=sub.language,
                source="opensubtitles",
                release=sub.release,
                score=round(score, 1),
                downloads=sub.downloads,
                fps=sub.fps,
            )
        return list(merged.values())

    # -- fetch ------------------------------------------------------------------

    def cues(self, candidate_id: str) -> list[tuple[float, float, str]]:
        """(start, end, text) of every cue of a subtitle, times in seconds."""
        return [
            (start / 1000, end / 1000, text)
            for start, end, text in parse_cues(self.get_vtt(candidate_id))
        ]

    def translated_vtt(
        self, candidate_id: str, target: str, engine: str, translator
    ) -> str:
        """The subtitle machine-translated into `target`, cached like the original."""
        cache_path = os.path.join(
            self.cache_dir,
            hashlib.sha1(f"{candidate_id}|{target}|{engine}".encode()).hexdigest()
            + ".vtt",
        )
        if os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as f:
                return f.read()
        cues = parse_cues(self.get_vtt(candidate_id))
        texts = translator.translate([text for _, _, text in cues], target, engine)
        out = ["WEBVTT", ""]
        for (start, end, _), text in zip(cues, texts, strict=True):
            out += [f"{_vtt_time(start)} --> {_vtt_time(end)}", text, ""]
        vtt = "\n".join(out)
        with open(cache_path, "w", encoding="utf-8") as f:
            f.write(vtt)
        return vtt

    def _download_org(self, id_subtitle: int, meta: SubtitleCandidate | None) -> bytes:
        """OpenSubtitles.org first (no quota); the same file via the .com API if refused."""
        fallback = meta.file_id if meta and meta.file_id and self.api.enabled else None
        if not self.org_blocked:
            try:
                return self.org.download(id_subtitle)
            except Exception as e:  # bot check, limit, network
                if "within.website" in str(e) or "captcha" in str(e).lower():
                    logger.warning(
                        "OpenSubtitles.org asks for a bot check: using the API for an hour"
                    )
                    self._org_blocked_until = time.monotonic() + 3600
                if not fallback:
                    raise
        if not fallback:
            raise RuntimeError("OpenSubtitles.org downloads are blocked right now")
        return self.api.download(fallback)

    def language_of(self, candidate_id: str) -> str | None:
        meta = self._meta.get(candidate_id)
        return meta.language if meta else None

    def get_vtt(self, candidate_id: str) -> str:
        """Return the subtitle as WebVTT, downloading and caching it on first use."""
        if not _ID_RE.fullmatch(candidate_id):
            raise ValueError("Neplatné ID titulků")
        cache_path = os.path.join(
            self.cache_dir, hashlib.sha1(candidate_id.encode()).hexdigest() + ".vtt"
        )
        if os.path.exists(cache_path):
            with open(cache_path, encoding="utf-8") as f:
                return f.read()

        meta = self._meta.get(candidate_id)
        language = meta.language if meta else None
        kind, _, rest = candidate_id.partition("-")
        if kind == "org":
            raw = self._download_org(int(rest), meta)
        elif kind == "os":
            if not self.api.enabled:
                raise RuntimeError("OpenSubtitles API není nakonfigurováno")
            raw = self.api.download(int(rest))
        else:
            info_hash, _, index = rest.partition("-")
            files = self.torrserver.files(info_hash, wait=10)
            file = next((f for f in files if f.index == int(index)), None)
            if file is None or not file.is_subtitle:
                raise ValueError("Soubor s titulky v torrentu nenalezen")
            raw = self.torrserver.read_file(info_hash, file)
            language = language or language_from_filename(file.path)

        vtt = to_webvtt(decode_subtitle(raw, language), fps=meta.fps if meta else None)
        if "-->" not in vtt:
            raise ValueError("Titulky se nepodařilo převést (neznámý formát)")
        tmp_path = cache_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(vtt)
        os.replace(tmp_path, cache_path)
        return vtt
