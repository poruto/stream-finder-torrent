"""Subtitle files: encodings, SRT / WebVTT / ASS / MicroDVD parsing and WebVTT output."""

import pytest

from streamfinder.subtitles.formats import (
    _clean_text,
    _vtt_time,
    decode_subtitle,
    is_advert,
    language_from_filename,
    opensubtitles_hash,
    parse_cues,
    to_webvtt,
)

CZECH = "Příliš žluťoučký kůň úpěl ďábelské ódy."
SLOVAK = "Ťažký deň, ľúbim ťa, dôležité."


def srt(*cues):
    blocks = [
        f"{i}\n{start} --> {end}\n{text}\n"
        for i, (start, end, text) in enumerate(cues, 1)
    ]
    return "\n".join(blocks)


# -- encodings --------------------------------------------------------------------------


@pytest.mark.parametrize("encoding", ["cp1250", "iso-8859-2", "utf-8"])
@pytest.mark.parametrize("language", ["cs", None])
def test_czech_in_every_common_encoding(encoding, language):
    assert decode_subtitle(CZECH.encode(encoding), language) == CZECH


@pytest.mark.parametrize("encoding", ["cp1250", "iso-8859-2"])
def test_slovak_legacy_encodings(encoding):
    assert decode_subtitle(SLOVAK.encode(encoding), "sk") == SLOVAK


def test_utf8_bom_is_removed():
    assert decode_subtitle(b"\xef\xbb\xbf" + CZECH.encode("utf-8"), "cs") == CZECH


def test_utf16_with_bom():
    assert decode_subtitle("Ahoj, světe".encode("utf-16"), "cs") == "Ahoj, světe"


def test_cyrillic_and_western_languages():
    assert decode_subtitle("Привет мир".encode("cp1251"), "ru") == "Привет мир"
    assert decode_subtitle("Grüße aus Köln".encode("cp1252"), "de") == "Grüße aus Köln"
    assert (
        decode_subtitle("Árvíztűrő tükörfúrógép".encode("iso-8859-2"), "hu")
        == "Árvíztűrő tükörfúrógép"
    )


def test_utf8_wins_over_legacy_guesses():
    text = "Ça va très bien, mañana"
    assert decode_subtitle(text.encode("utf-8"), "cs") == text


@pytest.mark.parametrize(
    "text, language", [("Ça va très bien.", "fr"), ("Perché è così?", "it")]
)
def test_western_legacy_subtitles_keep_their_accents(text, language):
    assert decode_subtitle(text.encode("cp1252"), language) == text


def test_polish_iso_8859_2():
    text = "Zażółć gęślą jaźń."
    assert decode_subtitle(text.encode("iso-8859-2"), "pl") == text


# -- SRT ---------------------------------------------------------------------------------


def test_srt_cues_in_milliseconds():
    text = srt(
        ("00:00:01,000", "00:00:02,500", "First"),
        ("01:02:03,004", "01:02:04,000", "Line one\nLine two"),
    )
    assert parse_cues(text) == [
        (1000, 2500, "First"),
        (3723004, 3724000, "Line one\nLine two"),
    ]


def test_srt_short_milliseconds_and_loose_hours():
    text = "1\n0:0:1,5 --> 0:0:2,25\nshort\n"
    assert parse_cues(text) == [(1500, 2250, "short")]


def test_srt_with_windows_and_mac_line_endings():
    text = srt(
        ("00:00:01,000", "00:00:02,000", "A"), ("00:00:03,000", "00:00:04,000", "B")
    )
    expected = [(1000, 2000, "A"), (3000, 4000, "B")]
    assert parse_cues(text.replace("\n", "\r\n")) == expected
    assert parse_cues(text.replace("\n", "\r")) == expected


def test_srt_without_blank_lines_between_cues():
    text = "1\n00:00:01,000 --> 00:00:02,000\nA\n2\n00:00:03,000 --> 00:00:04,000\nB\n"
    assert parse_cues(text) == [(1000, 2000, "A"), (3000, 4000, "B")]


def test_srt_with_bom_and_dot_separator():
    text = "﻿1\n00:00:01.000 --> 00:00:02.000\nA\n"
    assert parse_cues(text) == [(1000, 2000, "A")]


def test_empty_or_garbage_text_has_no_cues():
    assert parse_cues("") == []
    assert parse_cues("just some text\nwithout times") == []
    assert to_webvtt("garbage") == "WEBVTT\n"


# -- WebVTT ------------------------------------------------------------------------------


def test_webvtt_without_hours_settings_and_notes():
    text = (
        "WEBVTT\n\nNOTE written by hand\n\n"
        "00:01.000 --> 00:02.500 align:start position:10%\nHello\n\n"
        "cue-2\n01:00:00.000 --> 01:00:01.000\nAgain\n"
    )
    assert parse_cues(text) == [(1000, 2500, "Hello"), (3600000, 3601000, "Again")]


def test_webvtt_entities_are_not_escaped_twice():
    original = srt(("00:00:01,000", "00:00:02,000", "<i>Ahoj</i> & nazdar"))
    once = to_webvtt(original)
    assert "<i>Ahoj</i> &amp; nazdar" in once
    assert to_webvtt(once) == once


def test_srt_converted_once_escapes_ampersands():
    vtt = to_webvtt(srt(("00:00:01,000", "00:00:02,000", "Tom & Jerry")))
    assert vtt == "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nTom &amp; Jerry\n"


def test_webvtt_voice_spans_are_not_shown_as_text():
    vtt = to_webvtt("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<v Bob>Hi there</v>\n")
    assert "&lt;" not in vtt and "Hi there" in vtt


# -- ASS / SSA ---------------------------------------------------------------------------

ASS = r"""[Script Info]
Title: test
[V4+ Styles]
Format: Name, Fontname, Fontsize
Style: Default,Arial,20
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:05.00,0:00:06.00,Default,,0,0,0,,Second
Dialogue: 0,0:00:01.00,0:00:02.50,Default,,0,0,0,,{\an8}First, with a comma
Comment: 0,0:00:03.00,0:00:04.00,Default,,0,0,0,,not shown
Dialogue: 0,broken,0:00:04.00,Default,,0,0,0,,skipped
"""


def test_ass_dialogue_is_sorted_and_keeps_commas():
    assert parse_cues(ASS) == [
        (1000, 2500, r"{\an8}First, with a comma"),
        (5000, 6000, "Second"),
    ]


def test_ass_override_tags_and_line_breaks_are_cleaned():
    vtt = to_webvtt(ASS)
    assert "00:00:01.000 --> 00:00:02.500\nFirst, with a comma" in vtt
    assert "not shown" not in vtt and "skipped" not in vtt and "{" not in vtt


def test_ass_with_a_different_column_order():
    text = "[Events]\nFormat: Start, End, Text\nDialogue: 0:00:01.00,0:00:02.00,Hi\\Nthere\n"
    assert to_webvtt(text) == "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\nthere\n"


# -- MicroDVD ----------------------------------------------------------------------------


def test_microdvd_uses_the_given_fps_and_skips_the_fps_header():
    text = "{1}{1}25.000\n{25}{50}Radek 1|Radek 2\n{75}{100}Dalsi"
    assert parse_cues(text, fps=25) == [
        (1000, 2000, "Radek 1\nRadek 2"),
        (3000, 4000, "Dalsi"),
    ]


def test_microdvd_default_fps_is_film():
    assert parse_cues("{24}{48}A")[0][:2] == (1001, 2002)  # 23.976 fps


def test_microdvd_line_without_end_frame_is_shown():
    vtt = to_webvtt("{25}{50}First\n{75}{}Second", fps=25)
    assert "Second" in vtt


# -- cleaning ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, cleaned",
    [
        ("<b>Bold</b> <I>it</I> <u>u</u>", "<b>Bold</b> <i>it</i> <u>u</u>"),
        ("<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;"),
        ("a < b & c > d", "a &lt; b &amp; c &gt; d"),
        ('<font color="red">red</font>', "red"),
        (r"{\i1}styled{\i0}\Nsecond", "styled\nsecond"),
        ("one --> two", "one → two"),
        ("  padded  \n\n  lines  ", "padded\nlines"),
        ("", ""),
    ],
)
def test_clean_text(text, cleaned):
    assert _clean_text(text) == cleaned


@pytest.mark.parametrize(
    "ms, text",
    [
        (-5, "00:00:00.000"),
        (0, "00:00:00.000"),
        (3723004, "01:02:03.004"),
        (360000000, "100:00:00.000"),
    ],
)
def test_vtt_time(ms, text):
    assert _vtt_time(ms) == text


def test_to_webvtt_drops_adverts_empty_and_backwards_cues():
    text = srt(
        ("00:00:01,000", "00:00:02,000", "Watch at www.example.com"),
        ("00:00:03,000", "00:00:04,000", "real line"),
        ("00:00:05,000", "00:00:04,000", "backwards"),
        ("00:00:06,000", "00:00:06,000", "zero length"),
        ("00:00:07,000", "00:00:08,000", "   "),
    )
    assert to_webvtt(text) == "WEBVTT\n\n00:00:03.000 --> 00:00:04.000\nreal line\n"


@pytest.mark.parametrize(
    "text",
    [
        "Subtitles by XYZ",
        "Ray je vygeneruje okamžitě: getray.app",
        "Download at https://subs.example",
        "Support us and become VIP member",
        "OpenSubtitles.org",
        "Titulky přeložil Pepa",
    ],
)
def test_adverts(text):
    assert is_advert(text)


@pytest.mark.parametrize(
    "text",
    ["Go to the store.", "Mr. Smith, sit down.", "e.g. something", "Ahoj, jak se máš?"],
)
def test_not_adverts(text):
    assert not is_advert(text)


# -- languages and hashes ------------------------------------------------------------------


@pytest.mark.parametrize(
    "path, language",
    [
        ("Subs/3_Czech.srt", "cs"),
        ("Movie.2020.1080p.cze.srt", "cs"),
        ("cz.srt", "cs"),
        ("Movie.SDH.eng.srt", "en"),
        ("Movie.English.srt", "en"),
        ("Movie.slovak.srt", "sk"),
        ("Subs/Movie.ger.forced.srt", "de"),
        ("Movie.pt-BR.srt", "pt"),
        ("Sintel/Sintel.nl.srt", "nl"),
        ("Movie.2020.1080p.srt", None),
        ("x.srt", None),
        ("", None),
    ],
)
def test_language_from_filename(path, language):
    assert language_from_filename(path) == language


def test_moviehash_needs_two_full_chunks():
    chunk = b"\0" * 65536
    with pytest.raises(ValueError):
        opensubtitles_hash(1000, chunk, chunk)
    with pytest.raises(ValueError):
        opensubtitles_hash(10 * 65536, chunk[:-1], chunk)
    # All-zero data: the hash is the size itself.
    assert opensubtitles_hash(10 * 65536, chunk, chunk) == f"{10 * 65536:016x}"


def test_moviehash_wraps_at_64_bits():
    full = b"\xff" * 65536
    value = (2 * 65536 + 2 * 8192 * (2**64 - 1)) % 2**64
    assert opensubtitles_hash(2 * 65536, full, full) == f"{value:016x}"
    # Only the first / last 64 KiB count.
    longer = full + b"\x01" * 100
    assert (
        opensubtitles_hash(2 * 65536, longer, b"\x01" * 100 + full) == f"{value:016x}"
    )
