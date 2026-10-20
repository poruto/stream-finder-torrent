"""Subtitle text handling: encoding detection, format conversion to WebVTT,
language detection from file names and the OpenSubtitles moviehash."""

import re
import struct

LANGUAGE_NAMES = {
    "cs": "Čeština",
    "sk": "Slovenčina",
    "en": "English",
    "de": "Deutsch",
    "pl": "Polski",
    "hu": "Magyar",
    "fr": "Français",
    "es": "Español",
    "it": "Italiano",
    "ru": "Русский",
    "uk": "Українська",
    "pt": "Português",
}

# Tokens in subtitle file names that identify a language (bundled subs in torrents).
_LANGUAGE_TOKENS = {
    "cs": {"cs", "cz", "cze", "ces", "czech", "cesky", "cestina"},
    "sk": {"sk", "slo", "slk", "slovak", "slovensky"},
    "en": {"en", "eng", "english"},
    "de": {"de", "ger", "deu", "german"},
    "pl": {"pl", "pol", "polish"},
    "hu": {"hu", "hun", "hungarian"},
    "fr": {"fr", "fre", "fra", "french"},
    "es": {"es", "spa", "spanish"},
    "it": {"it", "ita", "italian"},
    "ru": {"ru", "rus", "russian"},
    "uk": {"uk", "ukr", "ukrainian"},
    "pt": {"pt", "por", "portuguese"},
}

_CENTRAL_EUROPEAN = {"cs", "sk", "pl", "hu", "sl", "hr", "ro"}
_CYRILLIC = {"ru", "uk", "bg", "sr"}
_CS_SK_LETTERS = set("áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽľĺŕôäĽĹŔÔÄ")
# What Czech letters turn into when cp1250 / ISO-8859-2 are confused.
_MOJIBAKE = set("¹¾»«©®ąĄ¼") | {chr(c) for c in range(0x80, 0xA0)}


def decode_subtitle(data: bytes, language: str | None = None) -> str:
    """Decode subtitle bytes, picking the right legacy codepage for the language."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass

    if language in _CYRILLIC:
        candidates = ["cp1251", "koi8-r"]
    elif language in _CENTRAL_EUROPEAN or language is None:
        candidates = ["cp1250", "iso-8859-2", "cp1252"]
    else:
        candidates = ["cp1252", "iso-8859-15", "cp1250"]

    def quality(text: str) -> float:
        letters = sum(ch in _CS_SK_LETTERS for ch in text)
        bad = sum(ch in _MOJIBAKE for ch in text)
        return letters - 5 * bad

    decoded = []
    for encoding in candidates:
        try:
            decoded.append((encoding, data.decode(encoding)))
        except UnicodeDecodeError:
            continue
    if not decoded:
        try:
            from charset_normalizer import from_bytes

            best = from_bytes(data).best()
            if best is not None:
                return str(best)
        except ImportError:
            pass
        return data.decode(candidates[0], errors="replace")
    if language in _CYRILLIC or len(decoded) == 1:
        return decoded[0][1]
    return max(decoded, key=lambda item: quality(item[1]))[1]


_SRT_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{1,2}):(\d{1,2})[,.:](\d{1,3})\s*-->\s*(\d{1,2}):(\d{1,2}):(\d{1,2})[,.:](\d{1,3})"
)
_VTT_TIME_RE = re.compile(
    r"(?:(\d{1,2}):)?(\d{1,2}):(\d{1,2})\.(\d{3})\s*-->\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{1,2})\.(\d{3})"
)
_MICRODVD_RE = re.compile(r"^\{(\d+)\}\{(\d*)\}(.*)$")
_ALLOWED_TAG_RE = re.compile(r"</?(?:i|b|u)>", re.IGNORECASE)


def _ms(h, m, s, ms) -> int:
    return ((int(h or 0) * 60 + int(m)) * 60 + int(s)) * 1000 + int(
        str(ms).ljust(3, "0")[:3]
    )


def _vtt_time(ms: int) -> str:
    ms = max(0, ms)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def _clean_text(text: str) -> str:
    text = re.sub(r"\{\\[^}]*\}", "", text)  # ASS override tags {\an8}
    text = re.sub(r"\\N|\\n", "\n", text)
    text = re.sub(r"<font[^>]*>|</font>", "", text, flags=re.IGNORECASE)
    # Escape everything except the tags WebVTT understands.
    parts = []
    last = 0
    for match in _ALLOWED_TAG_RE.finditer(text):
        parts.append(
            text[last : match.start()]
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        parts.append(match.group(0).lower())
        last = match.end()
    parts.append(
        text[last:].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    text = "".join(parts).replace("--&gt;", "→")
    return "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())


def parse_cues(text: str, fps: float | None = None) -> list[tuple]:
    """Parse SRT / WebVTT / MicroDVD / ASS into [(start_ms, end_ms, text)]."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    cues = []
    lines = text.split("\n")

    if any(_MICRODVD_RE.match(line.strip()) for line in lines[:5]):
        frame_rate = fps or 23.976
        for line in lines:
            match = _MICRODVD_RE.match(line.strip())
            if not match or match.group(1) == "1" and match.group(2) == "1":
                continue  # {1}{1}25.000 is an fps header
            start = int(int(match.group(1)) * 1000 / frame_rate)
            end = (
                int(int(match.group(2) or match.group(1)) * 1000 / frame_rate)
                or start + 2000
            )
            cues.append((start, end, match.group(3).replace("|", "\n")))
        return cues

    if re.search(r"^\[Events\]", text, re.MULTILINE):
        fmt = None
        for line in lines:
            if line.startswith("Format:") and fmt is None and "Start" in line:
                fmt = [f.strip().lower() for f in line[7:].split(",")]
            elif line.startswith("Dialogue:") and fmt:
                values = line[9:].split(",", len(fmt) - 1)
                row = dict(zip(fmt, (v.strip() for v in values)))
                try:
                    h1, m1, s1 = row["start"].split(":")
                    h2, m2, s2 = row["end"].split(":")
                    start = _ms(h1, m1, *s1.split("."))
                    end = _ms(h2, m2, *s2.split("."))
                except (KeyError, ValueError):
                    continue
                cues.append((start, end, row.get("text", "")))
        return sorted(cues)

    time_re = _VTT_TIME_RE if text.startswith("WEBVTT") else _SRT_TIME_RE
    i = 0
    while i < len(lines):
        match = time_re.search(lines[i])
        if not match:
            i += 1
            continue
        g = match.groups()
        start, end = _ms(*g[:4]), _ms(*g[4:])
        i += 1
        body = []
        while i < len(lines) and lines[i].strip():
            if time_re.search(lines[i]):
                break
            body.append(lines[i])
            i += 1
        # Drop a trailing SRT counter that got glued to the cue text.
        if (
            body
            and body[-1].strip().isdigit()
            and i < len(lines)
            and time_re.search(lines[i])
        ):
            body.pop()
        cues.append((start, end, "\n".join(body)))
    return cues


# Advert cues injected by subtitle sites ("Ray je vygeneruje okamžitě: getray.app").
_AD_RE = re.compile(
    r"(?:https?://|www\.|\b[\w-]+\.(?:app|com|org|net|cz|sk|io)\b|opensubtitles|osdb\.link|"
    r"advertise your product|become vip member|subtitles by|titulky (?:přeložil|stáhnete))",
    re.IGNORECASE,
)


def is_advert(text: str) -> bool:
    return bool(_AD_RE.search(text))


def to_webvtt(text: str, fps: float | None = None) -> str:
    """Convert subtitle text in any supported format to WebVTT, dropping advert cues."""
    out = ["WEBVTT", ""]
    for start, end, body in parse_cues(text, fps):
        cleaned = _clean_text(body)
        if not cleaned or end <= start or is_advert(cleaned):
            continue
        out.append(f"{_vtt_time(start)} --> {_vtt_time(end)}")
        out.append(cleaned)
        out.append("")
    return "\n".join(out)


HASH_CHUNK = 64 * 1024


def opensubtitles_hash(size: int, head: bytes, tail: bytes) -> str:
    """OpenSubtitles moviehash: size + 64-bit LE word sums of the first and last 64 KiB."""
    if size < HASH_CHUNK * 2 or len(head) < HASH_CHUNK or len(tail) < HASH_CHUNK:
        raise ValueError("File too small for moviehash")
    value = size
    for chunk in (head[:HASH_CHUNK], tail[-HASH_CHUNK:]):
        for (word,) in struct.iter_unpack("<Q", chunk):
            value = (value + word) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


def language_from_filename(path: str) -> str | None:
    tokens = [t for t in re.split(r"[^a-z0-9]+", path.lower()) if t]
    # Prefer tokens near the end ("Movie.2020.1080p.cze.srt", "Subs/3_Czech.srt").
    for token in reversed(tokens[:-1] if tokens else []):
        for code, words in _LANGUAGE_TOKENS.items():
            if token in words:
                return code
    # "Movie.nl.srt": an unknown two-letter code right before the extension is
    # still a language - report it so it gets filtered, rather than guessed as English.
    if len(tokens) >= 3 and re.fullmatch(r"[a-z]{2}", tokens[-2]):
        return tokens[-2]
    return None
