"""Release-name parsing and relevance scoring.

Shared by torrent search (is this torrent the right title/episode, and how good
is it for in-browser streaming?) and subtitle search (was this subtitle made
for the same release, so it will be in sync?).
"""

import functools
import math
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher

VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".m4v",
    ".ts",
    ".m2ts",
    ".mpg",
    ".mpeg",
}
SUBTITLE_EXTENSIONS = {".srt", ".vtt", ".ass", ".ssa", ".sub"}

_RESOLUTIONS = {
    "2160p": 2160,
    "4k": 2160,
    "uhd": 2160,
    "1080p": 1080,
    "1080i": 1080,
    "720p": 720,
    "576p": 576,
    "540p": 540,
    "480p": 480,
    "360p": 360,
}

# (canonical name, regex on the normalised dotted name); first match wins.
_SOURCES = [
    ("REMUX", r"remux"),
    ("BluRay", r"blu-?ray|bdrip|brrip|bdremux|\bbd(?:25|50)?\b"),
    ("WEB-DL", r"web-?dl"),
    ("WEBRip", r"web-?rip"),
    ("WEB", r"\bweb\b"),
    ("HDTV", r"hdtv|pdtv|dsr"),
    ("DVD", r"dvd-?rip|dvd(?:5|9)?\b"),
]
_CAM_TOKENS = {
    "cam",
    "camrip",
    "hdcam",
    "ts",
    "hdts",
    "telesync",
    "tc",
    "hdtc",
    "telecine",
    "scr",
    "screener",
    "dvdscr",
    "dvdscreener",
    "hdrip-ts",
}
_CODECS = [
    ("HEVC", r"x\.?265|h\.?265|hevc"),
    ("H264", r"x\.?264|h\.?264|avc"),
    ("AV1", r"\bav1\b"),
    ("XviD", r"xvid|divx"),
]
_AUDIO = [
    ("TrueHD", r"truehd|atmos"),
    ("DTS", r"dts"),
    ("EAC3", r"e-?ac-?3|ddp|dd\+"),
    ("AC3", r"\bac-?3\b|\bdd(?:2|5|7)?\b|dolby"),
    ("AAC", r"\baac"),
    ("MP3", r"\bmp3\b"),
    ("Opus", r"\bopus\b"),
]
_EDITIONS = {
    "extended",
    "uncut",
    "unrated",
    "directors",
    "dc",
    "theatrical",
    "imax",
    "remastered",
    "criterion",
}
_STREAMING_SERVICES = {
    "amzn",
    "nf",
    "dsnp",
    "hmax",
    "max",
    "atvp",
    "hulu",
    "pcok",
    "pmtp",
    "stan",
    "it",
    "cr",
    "sky",
    "skst",
    "now",
    "red",
    "vdl",
}
_CZ_TOKENS = {
    "cz",
    "cze",
    "czech",
    "cesky",
    "cestina",
    "dabing",
    "cztit",
    "cztitulky",
    "titulky",
}
_SK_TOKENS = {"sk", "slo", "slk", "slovak", "slovensky"}
_EN_TOKENS = {"en", "eng", "english"}

# Words that end the title part of a release name.
_STOP_TOKENS = (
    set(_RESOLUTIONS)
    | _CAM_TOKENS
    | _EDITIONS
    | _CZ_TOKENS  # "Avatar 3D CZ Extended Edition", "Pelisky CZ dabing"
    | _SK_TOKENS
    | {
        "3d",
        "bluray",
        "bdrip",
        "brrip",
        "webrip",
        "webdl",
        "web",
        "hdtv",
        "dvdrip",
        "remux",
        "x264",
        "x265",
        "h264",
        "h265",
        "hevc",
        "proper",
        "repack",
        "internal",
        "complete",
        "season",
        "hdr",
        "hdr10",
        "dv",
        "multi",
        "dual",
        "subs",
        "xvid",
        "hdrip",
        "10bit",
        "aac",
        "ac3",
        "dts",
        "limited",
        "dubbed",
    }
)

# Releases that are not the plain movie/episode or only pretend to be better quality.
_JUNK_TOKENS = {
    "rifftrax",
    "commentary",
    "fanedit",
    "upscaled",
    "upscale",
    "trailer",
    "featurette",
    "extras",
    "bonus",
}
_JUNK_RE = re.compile(
    r"\bai[ ._-]?(?:enhanced|upscaled?)\b|\bfan[ ._-]edit\b|\b(?:60|120)fps\b|\bmaking[ ._-]of\b"
)

_EPISODE_PATTERNS = [
    # S01E02, S01E02E03, S01E02-E05, S01.E02
    re.compile(
        r"\bs(\d{1,2})[ ._-]?e(\d{1,3})(?:(?:[ ._-]?e|-)(\d{1,3}))*", re.IGNORECASE
    ),
    # 1x02
    re.compile(r"\b(\d{1,2})x(\d{2,3})\b", re.IGNORECASE),
    # Season 1 Episode 2
    re.compile(
        r"\bseason[ ._-]?(\d{1,2})[ ._-]?episode[ ._-]?(\d{1,3})", re.IGNORECASE
    ),
]
_SEASON_RANGE_RE = re.compile(
    r"\bs(\d{1,2})[ ._-]?(?:-|to)[ ._-]?s?(\d{1,2})\b", re.IGNORECASE
)
_SEASON_RE = re.compile(
    r"\b(?:s|season[ ._-]?|series[ ._-]?)(\d{1,2})\b(?![ ._-]?e\d)", re.IGNORECASE
)
_SEASON_WORD_RANGE_RE = re.compile(
    r"\bseasons?[ ._-]?(\d{1,2})[ ._-]?(?:-|to|&)[ ._-]?(\d{1,2})\b", re.IGNORECASE
)
_EPISODE_ONLY_RE = re.compile(
    r"(?:^|[ ._-])(?:e|ep|episode)[ ._-]?(\d{1,3})\b", re.IGNORECASE
)
_YEAR_RE = re.compile(r"(?<![0-9])(19[2-9]\d|20[0-4]\d)(?![0-9p])")
_GROUP_RE = re.compile(r"-([A-Za-z0-9]{2,20})(?:\[[^\]]*\])?\s*$")
_LEADING_TAG_RE = re.compile(
    r"^\s*(?:\[[^\]]*\]|\{[^}]*\}|\([^)]*www\.[^)]*\)|www\.\S+\s+-)\s*[-._ ]*",
    re.IGNORECASE,
)
_SIZE_RE = re.compile(r"([\d.,]+)\s*([KMGT]i?B)", re.IGNORECASE)
# Sequel / part numbers in titles: "Scream 2", "Rocky II", "Dune: Part One".
_NUMBER_WORDS = {
    word: n
    for words in (
        "i ii iii iv v vi vii viii ix x",
        "one two three four five six seven eight nine ten eleven twelve",
    )
    for n, word in enumerate(words.split(), 1)
}
# Country tags that tell same-name shows apart: "The.Office.US", "Shameless.UK".
_COUNTRY_TAGS = {
    "us": "US",
    "usa": "US",
    "uk": "GB",
    "gb": "GB",
    "au": "AU",
    "aus": "AU",
    "nz": "NZ",
    "ca": "CA",
}


def normalize_title(text: str) -> str:
    """Lowercase, strip accents and punctuation: "Amélie: Les Fabuleux!" -> "amelie les fabuleux"."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"['’`]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def split_extension(name: str) -> tuple[str, str]:
    """Split a known media/subtitle extension off a file name."""
    match = re.search(r"(\.[A-Za-z0-9]{2,4})$", name)
    if match and match.group(1).lower() in VIDEO_EXTENSIONS | SUBTITLE_EXTENSIONS:
        return name[: match.start()], match.group(1).lower()
    return name, ""


def is_video_path(path: str) -> bool:
    return split_extension(path)[1] in VIDEO_EXTENSIONS


def is_subtitle_path(path: str) -> bool:
    return split_extension(path)[1] in SUBTITLE_EXTENSIONS


@dataclass
class ReleaseInfo:
    """Facts extracted from a torrent or file name."""

    raw: str
    title: str = ""
    year: int | None = None
    seasons: list[int] = field(default_factory=list)
    episodes: list[int] = field(default_factory=list)
    resolution: int | None = None
    source: str | None = None
    codec: str | None = None
    audio: str | None = None
    group: str | None = None
    service: str | None = None
    editions: set[str] = field(default_factory=set)
    tokens: set[str] = field(default_factory=set)
    is_cam: bool = False
    is_sample: bool = False
    is_complete: bool = False
    is_junk: bool = False  # commentary tracks, AI upscales, fan edits...
    has_czech: bool = False
    has_slovak: bool = False
    # The year sits in the title part ("Doctor.Who.2005.S01"), not after the episode marker.
    title_year: bool = False

    @property
    def season(self) -> int | None:
        return self.seasons[0] if len(self.seasons) == 1 else None

    @property
    def is_season_pack(self) -> bool:
        return bool(self.seasons) and not self.episodes

    @property
    def browser_friendly(self) -> bool | None:
        """Can a browser <video> play it *with sound*? True / False / None (unknown).

        Browsers decode AAC/MP3/Opus but not AC3, E-AC3 (DDP), DTS or TrueHD, which
        most Blu-ray and scene WEB releases use - the picture plays, the sound is
        silent. Releases that don't name their audio are usually DDP, so only an
        explicitly browser-safe audio track counts as True.
        """
        if self.codec == "XviD" or self.audio in ("AC3", "EAC3", "DTS", "TrueHD"):
            return False
        # HEVC (esp. 10-bit) only plays in some browsers with hardware support.
        if self.codec not in (None, "H264", "AV1"):
            return False
        if self.audio in ("AAC", "MP3", "Opus"):
            return True
        return None


def _name_part(raw: str) -> str:
    """The part of a name that describes the release.

    A path gives its file name; "Duna / Dune (2021)" the part with the year; and
    "Dune Part One 2021 1080p EN/ITA" must not be cut down to "ITA".
    """
    parts = [p.strip() for p in raw.replace("\\", "/").split("/") if p.strip()]
    if len(parts) < 2:
        return parts[0] if parts else ""
    if split_extension(parts[-1])[1]:
        return parts[-1]
    for part in reversed(parts):
        if _YEAR_RE.search(part):
            return part
    return max(parts, key=len)


def parse_release(name: str) -> ReleaseInfo:
    """Parse a scene-style release / file name into a ReleaseInfo."""
    raw = name or ""
    base, _ = split_extension(_name_part(raw))
    base = _LEADING_TAG_RE.sub("", base)
    info = ReleaseInfo(raw=raw)

    dotted = re.sub(r"[\s_]+", ".", base)
    lower = dotted.lower()
    tokens = _tokens(base)
    info.tokens = set(tokens)

    # Season / episode
    title_end = len(dotted)
    for pattern in _EPISODE_PATTERNS:
        match = pattern.search(dotted)
        if match:
            info.seasons = [int(match.group(1))]
            episodes = re.findall(r"\d+", match.group(0))[1:]
            if (
                pattern is _EPISODE_PATTERNS[0]
                and "-" in match.group(0)
                and len(episodes) == 2
            ):
                start, end = int(episodes[0]), int(episodes[1])
                info.episodes = (
                    list(range(start, end + 1))
                    if 0 < end - start < 50
                    else [start, end]
                )
            else:
                info.episodes = [int(e) for e in episodes]
            title_end = min(title_end, match.start())
            break
    if not info.seasons:
        range_match = _SEASON_RANGE_RE.search(dotted) or _SEASON_WORD_RANGE_RE.search(
            dotted
        )
        if range_match:
            start, end = int(range_match.group(1)), int(range_match.group(2))
            if 0 < start <= end < 100:
                info.seasons = list(range(start, end + 1))
            title_end = min(title_end, range_match.start())
        else:
            season_match = _SEASON_RE.search(dotted)
            if season_match:
                info.seasons = [int(season_match.group(1))]
                title_end = min(title_end, season_match.start())
    if not info.episodes:
        episode_match = _EPISODE_ONLY_RE.search(dotted)
        if episode_match and not _YEAR_RE.fullmatch(episode_match.group(1)):
            info.episodes = [int(episode_match.group(1))]
            title_end = min(title_end, episode_match.start())

    # Year: take the last plausible one so titles like "2001 A Space Odyssey 1968" work.
    years = list(_YEAR_RE.finditer(dotted))
    years = [y for y in years if y.start() > 0] or years
    if years:
        year_match = years[-1]
        info.year = int(year_match.group(1))
        info.title_year = year_match.start() < title_end
        title_end = min(title_end, year_match.start())

    for token in tokens:
        if token in _RESOLUTIONS:
            info.resolution = max(info.resolution or 0, _RESOLUTIONS[token])
    for canonical, pattern in _SOURCES:
        if re.search(pattern, lower):
            info.source = canonical
            break
    for canonical, pattern in _CODECS:
        if re.search(pattern, lower):
            info.codec = canonical
            break
    for canonical, pattern in _AUDIO:
        if re.search(pattern, lower):
            info.audio = canonical
            break

    info.is_cam = bool(info.tokens & _CAM_TOKENS) and info.source not in (
        "BluRay",
        "REMUX",
        "WEB-DL",
    )
    info.is_sample = "sample" in info.tokens
    info.is_complete = bool(info.tokens & {"complete", "collection"})
    info.is_junk = bool(info.tokens & _JUNK_TOKENS) or bool(_JUNK_RE.search(lower))
    info.editions = info.tokens & _EDITIONS
    info.has_czech = bool(info.tokens & _CZ_TOKENS)
    info.has_slovak = bool(info.tokens & _SK_TOKENS)
    service = info.tokens & _STREAMING_SERVICES
    if service and info.source in ("WEB-DL", "WEBRip", "WEB"):
        info.service = sorted(service)[0]

    group_match = _GROUP_RE.search(base)
    if group_match and group_match.group(1).lower() not in _STOP_TOKENS:
        info.group = group_match.group(1).lower()

    # Title is everything before the first structural marker.
    title_part = dotted[:title_end]
    title_tokens = []
    for token in re.split(r"[.\-\[\]()]+", title_part):
        if token.lower() in _STOP_TOKENS:
            break
        title_tokens.append(token)
    info.title = normalize_title(" ".join(title_tokens))
    return info


def title_similarity(candidate: str, expected_titles: Iterable[str]) -> float:
    """Return 0..1 similarity between a parsed release title and any expected title."""
    cand_variants = _title_variants(candidate)
    if not cand_variants:
        return 0.0
    best = 0.0
    for expected in expected_titles:
        for exp in _title_variants(expected):
            for cand in cand_variants:
                if exp == cand:
                    return 1.0
                # "Title" vs "Title US" / "Title Show" style suffixes
                if cand.startswith(exp + " ") and len(cand) - len(exp) <= 4:
                    best = max(best, 0.9)
                matcher = SequenceMatcher(None, exp, cand)
                # ratio() is slow; its cheap upper bounds skip pairs that can't beat `best`.
                if matcher.real_quick_ratio() > best and matcher.quick_ratio() > best:
                    best = max(best, matcher.ratio())
    return best


@functools.lru_cache(maxsize=4096)
def _title_variants(title: str | None) -> tuple[str, ...]:
    """A title normalised, without a leading article and without "and".

    Cached: one search compares hundreds of release names with the same few titles.
    """
    norm = normalize_title(title)
    if not norm:
        return ()
    variants = (norm, re.sub(r"^(the|a|an) ", "", norm), norm.replace(" and ", " "))
    return tuple(dict.fromkeys(variants))


def _title_numbers(title: str) -> set[int]:
    return {
        int(w) if w.isdigit() else _NUMBER_WORDS[w]
        for w in normalize_title(title).split()
        if w.isdigit() or w in _NUMBER_WORDS
    }


def same_numbers(candidate: str, expected_titles: Iterable[str]) -> bool:
    """Whether the sequel/part numbers agree with one of the expected titles.

    The fuzzy title match lets "Scream 2" pass for "Scream" (and "Halloween II" for
    "Halloween") - a different film, often only a year apart.
    """
    numbers = _title_numbers(candidate)
    return any(_title_numbers(t) == numbers for t in expected_titles)


def country_tag(candidate: str, expected_titles: Iterable[str]) -> str | None:
    """Country a show's release is tagged with ("The Office UK" -> "GB"), else None.

    A word that belongs to the show's own title ("This Is Us") is not a tag.
    """
    words = normalize_title(candidate).split()
    if len(words) < 2 or words[-1] not in _COUNTRY_TAGS:
        return None
    if any(normalize_title(t).split()[-1:] == words[-1:] for t in expected_titles):
        return None
    return _COUNTRY_TAGS[words[-1]]


def parse_size(value) -> int:
    """Parse '1.4 GB' / 1503238553 into bytes."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    match = _SIZE_RE.search(text)
    if not match:
        return 0
    number = float(match.group(1).replace(",", "."))
    unit = match.group(2).upper().replace("I", "")
    power = {"KB": 1, "MB": 2, "GB": 3, "TB": 4}[unit]
    return int(number * 1024**power)


def format_size(size_bytes: int) -> str:
    if not size_bytes:
        return "?"
    size = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


# ---------------------------------------------------------------------------
# Torrent scoring
# ---------------------------------------------------------------------------


@dataclass
class MatchTarget:
    """What the user wants to watch."""

    titles: list[str]
    media_type: str = "movie"
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    free_text: bool = False  # manual query: don't gate on title/episode
    preferred_resolution: int | None = None  # None = automatic (balanced 1080p)
    # TMDB knows another movie/show of the same title (remake, US/UK version):
    # a release name then needs the right year (or IMDb id) to count.
    ambiguous: bool = False
    countries: list[str] = field(default_factory=list)  # a show's origin: US, GB...


# Resolution bonus per user preference ("other" = known but lower resolution).
_RESOLUTION_BONUS: dict[int | None, dict] = {
    None: {2160: 8, 1080: 12, 720: 8, "other": 2},
    2160: {2160: 18, 1080: 10, 720: 3, "other": 0},
    1080: {2160: 5, 1080: 15, 720: 6, "other": 1},
    720: {2160: -8, 1080: 6, 720: 15, "other": 6},
}


def pack_kind(info: ReleaseInfo) -> str | None:
    """'season' | 'multi' (several seasons) | 'complete' | None (single item)."""
    if info.is_season_pack:
        return "season" if len(info.seasons) == 1 else "multi"
    if info.is_complete and not info.episodes:
        return "complete"
    return None


def score_torrent(
    info: ReleaseInfo,
    seeders: int,
    size_bytes: int,
    target: MatchTarget,
    id_match: bool = False,
    id_mismatch: bool = False,
) -> float | None:
    """Score a torrent for the target; None means 'wrong content, drop it'.

    `id_match` marks results a provider found by exact IMDb id, so naming
    differences ("Dune" vs "Dune: Part One") must not filter them out.
    `id_mismatch` marks results the index tagged with another IMDb id: a remake
    or a namesake ("Dune" 1984 when the 2021 film is wanted).
    """
    if info.is_sample:
        return None
    # Uploaders pick the IMDb tag by hand and sometimes take the original film's:
    # only an exact year in the name outweighs it ("Halloween.2018" tagged as 1978).
    if id_mismatch and not (target.year and info.year == target.year):
        return None
    score = -15.0 if id_mismatch else 0.0
    pack = pack_kind(info)

    if not target.free_text:
        similarity = 1.0 if id_match else title_similarity(info.title, target.titles)
        if similarity < 0.8:
            return None
        if not id_match and not same_numbers(info.title, target.titles):
            return None  # a sequel or prequel of the title
        score += (similarity - 0.8) * 50  # up to +10 for exact title

        if target.media_type == "movie":
            if info.seasons or info.episodes:
                return None
            if target.year and info.year:
                diff = abs(target.year - info.year)
                if diff > 1:
                    return None
                score += 10 if diff == 0 else 3
            elif target.year and target.ambiguous and not id_match:
                return None  # "Dune 1080p": the 1984 or the 2021 one?
        else:
            year_off = target.year and info.year and abs(target.year - info.year) > 1
            if year_off and not info.seasons:
                return (
                    None  # a year without season markers is probably a same-name movie
                )
            if year_off and info.title_year:
                # "Doctor.Who.2005.S01" when the 1963 show is wanted
                if target.ambiguous and not id_match:
                    return None
                score -= 15
            elif target.year and info.year and info.title_year:
                score += 5
            tag = country_tag(info.title, target.titles)
            if tag and target.countries and not id_match:
                if tag not in target.countries:
                    return None  # "The Office UK" when the US show is wanted
                score += 10  # also makes up for the tag lowering the title similarity
            if target.season is not None and target.episode is not None:
                if info.episodes:
                    if (
                        info.season != target.season
                        or target.episode not in info.episodes
                    ):
                        return None
                    score += 30 if len(info.episodes) == 1 else 20
                elif pack == "season":
                    if target.season not in info.seasons:
                        return None
                    score += 12
                elif pack == "multi":
                    if target.season not in info.seasons:
                        return None
                    score += 4
                elif pack == "complete":
                    score += 2
                else:
                    return None
            elif target.season is not None:
                if info.seasons and target.season not in info.seasons:
                    return None
                if pack == "season":
                    score += 25
                elif pack == "multi":
                    score += 10
                elif pack == "complete":
                    score += 6
                elif info.episodes:
                    score += 2
                else:
                    return None
            elif pack:
                score += 8

    if info.is_cam:
        score -= 40
    if info.is_junk:
        score -= 30

    bonus = _RESOLUTION_BONUS.get(target.preferred_resolution, _RESOLUTION_BONUS[None])
    score += bonus.get(info.resolution or 0, bonus["other"] if info.resolution else 4)
    score += {
        "REMUX": -4,
        "BluRay": 8,
        "WEB-DL": 8,
        "WEBRip": 6,
        "WEB": 6,
        "HDTV": 3,
        "DVD": 1,
    }.get(info.source or "", 0)
    if info.codec == "H264":
        score += 4
    elif info.codec == "XviD":
        score -= 4
    if info.audio == "AAC":
        score += 4
    elif info.audio in ("AC3", "EAC3", "DTS", "TrueHD"):
        score -= 3
    if info.browser_friendly is True:
        score += 6

    # Availability matters most for streaming: log-scale so 1000 seeders isn't 100x better than 10.
    score += -25 if seeders <= 0 else min(30.0, 10 * math.log10(seeders + 1))

    # Size sanity for a single movie/episode
    if size_bytes and not pack:
        gb = size_bytes / 1024**3
        if target.preferred_resolution == 720 and gb > 2.5:
            score -= 6
        if target.media_type == "movie":
            if gb > 30:
                score -= 8
            elif (info.resolution or 0) >= 1080 and gb < 0.5:
                score -= 6
        elif gb > 10:
            score -= 5

    return round(score, 1)


# ---------------------------------------------------------------------------
# Subtitle <-> video release matching
# ---------------------------------------------------------------------------


def _source_family(source: str | None) -> str | None:
    if source in ("WEB-DL", "WEBRip", "WEB"):
        return "WEB"
    if source in ("BluRay", "REMUX"):
        return "BluRay"
    return source


def release_match_score(subtitle_release: str, video: ReleaseInfo) -> float:
    """How likely a subtitle made for `subtitle_release` is in sync with `video` (0..~70)."""
    best = 0.0
    # OpenSubtitles release fields often list several releases.
    for variant in re.split(r"\s*(?:/|\||,|;|\n)\s*", subtitle_release or ""):
        if not variant.strip():
            continue
        sub = parse_release(variant)
        score = 0.0
        if video.group and sub.group and video.group == sub.group:
            score += 30
        elif video.group and video.group in sub.tokens:
            score += 22
        if video.source and sub.source:
            if video.source == sub.source:
                score += 15
            elif _source_family(video.source) == _source_family(sub.source):
                score += 8
            else:
                score -= 10
        if video.service and video.service == sub.service:
            score += 8
        if video.resolution and sub.resolution == video.resolution:
            score += 4
        if video.editions != sub.editions and (video.editions or sub.editions):
            score -= 12
        if (
            video.episodes
            and sub.episodes
            and not set(video.episodes) & set(sub.episodes)
        ):
            score -= 40
        common = video.tokens & sub.tokens
        union = video.tokens | sub.tokens
        if union:
            score += 10 * len(common) / len(union)
        best = max(best, score)
    return best
