"""OpenSubtitles.org (legacy site) - search via its XML pages, download without an API key.

The .com REST API allows only 5 downloads a day without login (20 with a free
account), while the legacy site serves the same subtitle archive without that
quota. The .com API reports `legacy_subtitle_id` for each subtitle, which is the
IDSubtitle used here, so .com search results can be downloaded through .org too.
"""

import io
import logging
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass

import requests

from ..matching import SUBTITLE_EXTENSIONS, split_extension

logger = logging.getLogger(__name__)

# OpenSubtitles.org uses ISO 639-2/B codes in its URLs.
_ISO639_2 = {
    "cs": "cze",
    "sk": "slo",
    "en": "eng",
    "de": "ger",
    "pl": "pol",
    "hu": "hun",
    "fr": "fre",
    "es": "spa",
    "it": "ita",
    "ru": "rus",
    "uk": "ukr",
    "pt": "por",
}


@dataclass
class OrgSubtitle:
    id_subtitle: int
    language: str
    release: str
    downloads: int
    fps: float | None
    cds: int
    format: str


class OpenSubtitlesOrgClient:
    BASE_URL = "https://www.opensubtitles.org"
    DOWNLOAD_URL = "https://dl.opensubtitles.org"

    def __init__(
        self, user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0"
    ):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent

    def search(self, imdb_id: str, languages: list[str]) -> list[OrgSubtitle]:
        """Subtitles for a movie or a single episode (use the episode's own IMDb id)."""
        codes = ",".join(_ISO639_2[lang] for lang in languages if lang in _ISO639_2)
        if not codes or not imdb_id:
            return []
        imdb = int(str(imdb_id).removeprefix("tt"))
        response = self.session.get(
            f"{self.BASE_URL}/en/search/sublanguageid-{codes}/imdbid-{imdb}/xml",
            timeout=15,
        )
        response.raise_for_status()
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError:
            # Titles .org doesn't know come back as an empty body or an HTML page.
            logger.info("OpenSubtitles.org has nothing for %s", imdb_id)
            return []

        results = []
        for node in root.iter("subtitle"):
            id_subtitle = node.findtext("IDSubtitle")
            if not id_subtitle:
                continue  # series overview rows have no subtitle id
            try:
                fps = float(node.findtext("MovieFPS") or 0) or None
            except ValueError:
                fps = None
            results.append(
                OrgSubtitle(
                    id_subtitle=int(id_subtitle),
                    language=(node.findtext("ISO639") or "").lower(),
                    release=node.findtext("MovieReleaseName") or "",
                    downloads=int(node.findtext("SubDownloadsCnt") or 0),
                    fps=fps,
                    cds=int(node.findtext("SubSumCD") or 1),
                    format=(node.findtext("SubFormat") or "srt").lower(),
                )
            )
        return results

    def download(self, id_subtitle: int) -> bytes:
        """Download the subtitle archive and return the subtitle file inside it."""
        response = self.session.get(
            f"{self.DOWNLOAD_URL}/en/download/sub/{int(id_subtitle)}", timeout=30
        )
        response.raise_for_status()
        data = response.content
        if not data.startswith(b"PK"):
            # A HTML page instead of a zip means captcha / download limit.
            raise RuntimeError("OpenSubtitles.org odmítl stažení (limit nebo captcha)")
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = [
                n
                for n in archive.namelist()
                if split_extension(n)[1] in SUBTITLE_EXTENSIONS
            ]
            if not names:
                raise RuntimeError("Archiv s titulky neobsahuje titulky")
            biggest = max(names, key=lambda n: archive.getinfo(n).file_size)
            return archive.read(biggest)
