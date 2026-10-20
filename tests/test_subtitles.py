import struct

from streamfinder.subtitles import (
    decode_subtitle,
    language_from_filename,
    opensubtitles_hash,
    to_webvtt,
)
from streamfinder.torrserver import TorrentFile, extract_info_hash, select_video_file

CZECH = "Příliš žluťoučký kůň úpěl ďábelské ódy. Šťastná Žofie."


def test_decode_czech_legacy_encodings():
    assert decode_subtitle(CZECH.encode("cp1250"), "cs") == CZECH
    assert decode_subtitle(CZECH.encode("iso-8859-2"), "cs") == CZECH
    assert decode_subtitle(CZECH.encode("utf-8"), "cs") == CZECH
    assert decode_subtitle(b"\xef\xbb\xbf" + CZECH.encode("utf-8"), "cs") == CZECH


def test_srt_to_vtt_escapes_and_keeps_formatting():
    srt = "1\r\n00:00:01,500 --> 00:00:03,000\r\n<i>Ahoj</i> <font color=red>a & b</font>\r\n\r\n2\r\n00:01:00,000 --> 00:01:02,250\r\n{\\an8}Nahoře\r\n"
    vtt = to_webvtt(srt)
    assert vtt.startswith("WEBVTT")
    assert "00:00:01.500 --> 00:00:03.000\n<i>Ahoj</i> a &amp; b" in vtt
    assert "00:01:00.000 --> 00:01:02.250\nNahoře" in vtt


def test_microdvd_and_ass():
    assert "00:00:01.000 --> 00:00:02.000\nRadek 1\nRadek 2" in to_webvtt(
        "{25}{50}Radek 1|Radek 2", fps=25
    )
    ass = "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,Ahoj\\Nsvete, jo\n"
    assert "00:00:01.000 --> 00:00:02.500\nAhoj\nsvete, jo" in to_webvtt(ass)


def test_opensubtitles_hash_matches_reference_algorithm():
    head = bytes(range(256)) * 256
    tail = bytes(reversed(range(256))) * 256
    size = 10 * 1024 * 1024
    expected = size
    for chunk in (head, tail):
        for i in range(0, len(chunk), 8):
            expected = (expected + struct.unpack("<Q", chunk[i : i + 8])[0]) % 2**64
    assert opensubtitles_hash(size, head, tail) == f"{expected:016x}"


def test_language_from_filename():
    assert language_from_filename("Subs/3_Czech.srt") == "cs"
    assert language_from_filename("Movie.2020.1080p.cze.srt") == "cs"
    assert language_from_filename("Sintel/Sintel.en.srt") == "en"
    assert language_from_filename("Sintel/Sintel.nl.srt") == "nl"
    assert language_from_filename("Movie.2020.1080p.srt") is None


def test_select_video_file_picks_episode_from_pack_and_skips_extras():
    files = [
        TorrentFile(1, "Show.S01/Show.S01E01.mkv", 900_000_000),
        TorrentFile(2, "Show.S01/Show.S01E02.mkv", 800_000_000),
        TorrentFile(3, "Show.S01/Sample/sample.mkv", 50_000_000),
        TorrentFile(4, "Show.S01/Show.S01E02.cs.srt", 40_000),
    ]
    assert select_video_file(files, season=1, episode=2).index == 2
    movie = [
        TorrentFile(1, "Movie/Movie.de.srt", 1652),
        TorrentFile(2, "Movie/Movie.mp4", 129_000_000),
    ]
    assert select_video_file(movie).index == 2


def test_extract_info_hash():
    assert (
        extract_info_hash(
            "magnet:?xt=urn:btih:08ADA5A7A6183AAE1E09D831DF6748D566095A10&dn=x"
        )
        == "08ada5a7a6183aae1e09d831df6748d566095a10"
    )
    assert (
        len(extract_info_hash("magnet:?xt=urn:btih:BCWX2UQYYNVK4HQJ5YNRS5WSRKO2CFIQ"))
        == 40
    )
    assert extract_info_hash("not a magnet") is None
