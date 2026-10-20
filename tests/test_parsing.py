"""Release-name parsing: what a torrent / file name tells about the video inside."""

import pytest

from streamfinder.matching import (
    format_size,
    is_subtitle_path,
    is_video_path,
    normalize_title,
    pack_kind,
    parse_release,
    parse_size,
    split_extension,
)
from streamfinder.torrents.search import czech_bonus, czech_kind

# -- resolution / source / codec / audio ---------------------------------------------


@pytest.mark.parametrize(
    "name, resolution",
    [
        ("Movie.2019.2160p.WEB-DL", 2160),
        ("Movie 2019 4K HDR10 WEB-DL", 2160),
        ("Movie.2019.UHD.BluRay", 2160),
        ("Movie.2019.1080p.BluRay", 1080),
        ("Movie.2019.1080i.HDTV", 1080),
        ("Movie.2019.720p.WEBRip", 720),
        ("Movie.2019.576p.DVDRip", 576),
        ("Movie.2019.480p.WEB", 480),
        ("Movie.2019.360p.WEB", 360),
        ("Movie.2019.HDRip.x264", None),
        ("Movie (2019) [1080p] [WEBRip] [YTS.MX]", 1080),
    ],
)
def test_resolution(name, resolution):
    assert parse_release(name).resolution == resolution


def test_highest_resolution_wins_when_several_are_named():
    assert parse_release("Movie.2019.720p.1080p.WEB").resolution == 1080


@pytest.mark.parametrize(
    "name, source",
    [
        ("Movie.2019.2160p.UHD.BluRay.REMUX.HEVC-FGT", "REMUX"),
        ("Movie.2019.1080p.BluRay.x264", "BluRay"),
        ("Movie.2019.BDRip.x264", "BluRay"),
        ("Movie.2019.720p.BRRip.x264", "BluRay"),
        ("Movie.2019.BD50", "BluRay"),
        ("Movie.2019.1080p.WEB-DL.H264", "WEB-DL"),
        ("Movie.2019.WEBDL.1080p", "WEB-DL"),
        ("Movie.2019.1080p.WEBRip.x264", "WEBRip"),
        ("[TGx] Movie.2019.1080p.WEB.h264-ETHEL", "WEB"),
        ("Show.S01E01.HDTV.x264", "HDTV"),
        ("Movie.2019.DVDRip.XviD", "DVD"),
        ("Movie.2019.DVD9", "DVD"),
        ("Movie.2019.1080p.x264", None),
    ],
)
def test_source(name, source):
    assert parse_release(name).source == source


@pytest.mark.parametrize(
    "name, codec",
    [
        ("Movie.2019.1080p.x265", "HEVC"),
        ("Movie.2019.1080p.H.265", "HEVC"),
        ("Movie.2019.1080p.HEVC.10bit", "HEVC"),
        ("Movie.2019.1080p.x264", "H264"),
        ("Movie.2019.1080p.H.264", "H264"),
        ("Movie.2019.1080p.BluRay.AVC", "H264"),
        ("Movie.2019.720p.WEB-DL.AV1", "AV1"),
        ("Movie.2019.DVDRip.XviD", "XviD"),
        ("Movie.2019.DVDRip.DivX", "XviD"),
        ("Movie.2019.1080p.WEB", None),
    ],
)
def test_codec(name, codec):
    assert parse_release(name).codec == codec


@pytest.mark.parametrize(
    "name, audio",
    [
        ("Movie.2019.2160p.BluRay.TrueHD.7.1", "TrueHD"),
        ("Movie.2019.2160p.WEB-DL.DDP5.1.Atmos", "TrueHD"),  # Atmos named: TrueHD-class
        ("Movie.2019.1080p.BluRay.DTS-HD.MA.5.1", "DTS"),
        ("Movie.2019.1080p.WEB-DL.DDP5.1.H.264", "EAC3"),
        ("Movie.2019.1080p.WEB.H264.EAC3", "EAC3"),
        ("Movie.2019.1080p.BluRay.AC3.x264", "AC3"),
        ("Movie.2019.1080i.HDTV.DD5.1", "AC3"),
        ("Movie.2019.1080p.WEBRip.x264.AAC", "AAC"),
        ("Movie.2019.1080p.WEBRip.AAC5.1", "AAC"),
        ("Movie.2019.DVDRip.XviD.MP3", "MP3"),
        ("Movie.2019.720p.WEB-DL.AV1.Opus", "Opus"),
        ("Movie.2019.1080p.WEB-DL.x264", None),
    ],
)
def test_audio(name, audio):
    assert parse_release(name).audio == audio


# -- browser playback ------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, friendly",
    [
        ("Movie.2019.1080p.WEBRip.x264.AAC-YTS", True),
        ("Movie.2019.720p.WEB-DL.AV1.Opus", True),
        ("Movie.2019.480p.WEB.MP3", True),
        ("Movie.2019.1080p.WEB-DL.DDP5.1.H.264-NTb", False),  # silent in a browser
        ("Movie.2019.1080p.BluRay.DTS.x264", False),
        ("Movie.2019.1080p.WEBRip.x265.AAC", False),  # HEVC: hardware dependent
        ("Movie.2019.DVDRip.XviD.MP3", False),
        ("Movie.2019.1080p.WEB-DL.x264", None),  # audio not named: unknown
    ],
)
def test_browser_friendly(name, friendly):
    assert parse_release(name).browser_friendly is friendly


# -- seasons and episodes --------------------------------------------------------------


@pytest.mark.parametrize(
    "name, seasons, episodes",
    [
        ("Show.S01E02.720p", [1], [2]),
        ("Show.s01e02.720p", [1], [2]),
        ("Show.S1E1.720p", [1], [1]),
        ("Show.S01.E02.720p", [1], [2]),
        ("Show.S01E100.720p", [1], [100]),
        ("Show.S02E05E06.720p", [2], [5, 6]),
        ("Show.S03E07E08E09.720p", [3], [7, 8, 9]),
        ("Show.S01E01-E03.720p.HDTV", [1], [1, 2, 3]),
        ("Show.S01E01-03.720p", [1], [1, 2, 3]),
        ("Show.1x05.HDTV", [1], [5]),
        ("Show.10x125.HDTV", [10], [125]),
        ("Show Season 2 Episode 7 720p", [2], [7]),
        ("Show.E05.720p", [], [5]),
        ("Show.Ep.12.HDTV", [], [12]),
        ("Show.S02.1080p", [2], []),
        ("Show.Series.3.720p", [3], []),
        ("Show Season 2 Complete 1080p", [2], []),
        ("Show.S01-S05.Complete.1080p", [1, 2, 3, 4, 5], []),
        ("The.Expanse.S01-S06.1080p", [1, 2, 3, 4, 5, 6], []),
        ("Show.Seasons.1-3.1080p", [1, 2, 3], []),
        ("Hello Kitty Show Season 1-3", [1, 2, 3], []),
        ("Movie.2019.1080p.BluRay", [], []),
    ],
)
def test_seasons_and_episodes(name, seasons, episodes):
    info = parse_release(name)
    assert (info.seasons, info.episodes) == (seasons, episodes)


def test_long_episode_range_keeps_only_its_ends():
    # 60 episodes "S01E01-E60" is no plausible range: only both ends are kept.
    assert parse_release("Show.S01E01-E60.720p").episodes == [1, 60]


def test_season_property_only_for_a_single_season():
    assert parse_release("Show.S02E03").season == 2
    assert parse_release("Show.S01-S03.1080p").season is None
    assert parse_release("Movie.2019.1080p").season is None


@pytest.mark.parametrize(
    "name, kind",
    [
        ("Show.S02.1080p", "season"),
        ("Show Season 2 Complete 1080p", "season"),
        ("Show.S01-S05.Complete.1080p", "multi"),
        ("Show.Seasons.1-3.1080p", "multi"),
        ("Show.Complete.Series.1080p", "complete"),
        ("Show.Collection.1080p", "complete"),
        ("Show.S02E03.1080p", None),
        ("Show.S02E01-E05.1080p", None),
        ("Movie.2019.1080p", None),
    ],
)
def test_pack_kind(name, kind):
    assert pack_kind(parse_release(name)) == kind


def test_episode_in_a_folder_path_is_read_from_the_file_name():
    info = parse_release("Show (2010)/Season 3/Show.S03E07.1080p.WEB.mkv")
    assert info.seasons == [3] and info.episodes == [7] and info.title == "show"


# -- titles and years ------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, title, year",
    [
        ("Se7en.1995.1080p", "se7en", 1995),
        ("Ocean's.Eleven.2001.1080p", "oceans eleven", 2001),
        ("Wall-E.2008.1080p", "wall e", 2008),
        ("Spider-Man.No.Way.Home.2021.1080p", "spider man no way home", 2021),
        ("2012.2009.1080p.BluRay", "2012", 2009),
        ("1984.1984.1080p", "1984", 1984),
        ("1917.2019.1080p.BluRay.x264", "1917", 2019),
        ("2001.A.Space.Odyssey.1968.1080p", "2001 a space odyssey", 1968),
        ("Taxi Driver (1976) 1080p", "taxi driver", 1976),
        ("Toy.Story.3.2010.1080p", "toy story 3", 2010),
        ("Rocky.IV.1985.1080p", "rocky iv", 1985),
        ("Amélie.2001.1080p", "amelie", 2001),
        ("Fast & Furious 2009 1080p", "fast and furious", 2009),
        ("Movie.1080p.2019", "movie", 2019),
        ("The 100 S01E01 720p", "the 100", None),
        ("24.S01E01.720p", "24", None),
        ("Movie.19.1080p", "movie 19", None),
        ("Interstellar.2014.IMAX.1080p", "interstellar", 2014),
    ],
)
def test_title_and_year(name, title, year):
    info = parse_release(name)
    assert (info.title, info.year) == (title, year)


@pytest.mark.parametrize(
    "name",
    [
        "[TGx] Movie.2019.1080p.WEB.h264-ETHEL",
        "www.Torrenting.com - Movie.2019.720p.BRRip.x264.AAC-ETRG",
        "{Group} Movie 2019 1080p",
    ],
)
def test_site_tags_in_front_are_not_the_title(name):
    assert parse_release(name).title == "movie"


@pytest.mark.parametrize(
    "name",
    [
        "Movie.2019.PROPER.1080p",
        "Movie.2019.REPACK.1080p",
        "Movie.2019.MULTi.1080p",
        "Movie.2019.DUAL.1080p",
        "Movie.2019.HDR.2160p",
        "Movie.2019.Extended.1080p",
        "Movie.2019.CZ.dabing",
    ],
)
def test_title_stops_at_release_words(name):
    assert parse_release(name).title == "movie"


def test_title_stops_at_release_words_even_without_a_year():
    assert parse_release("Avatar 3D CZ Extended Edition").title == "avatar"
    assert parse_release("Movie Title 1080p BluRay").title == "movie title"


def test_year_in_the_title_part_of_a_show():
    revival = parse_release("Doctor.Who.2005.S01E01.720p")
    assert revival.year == 2005 and revival.title_year and revival.title == "doctor who"
    aired = parse_release("Show.S01E01.2005.720p")
    assert aired.year == 2005 and not aired.title_year


def test_years_outside_the_plausible_range_are_not_years():
    assert parse_release("Movie.1899.1080p").year is None
    assert parse_release("Movie.2050.1080p").year is None
    # "1080p" is a resolution, never the year 1080.
    assert parse_release("Movie.1080p").year is None


# -- groups, services, editions ------------------------------------------------------------


@pytest.mark.parametrize(
    "name, group",
    [
        ("Movie.2019.1080p.WEB-DL.DDP5.1.H.264-NTb", "ntb"),
        ("Movie.2019.2160p.UHD.BluRay.REMUX.HDR.HEVC.TrueHD.Atmos.7.1-FGT", "fgt"),
        ("Movie.2019.1080p.x264-GRP[rarbg]", "grp"),
        ("Movie.2019.1080p.BluRay-x264", None),  # a codec, not a group
        ("Movie 2019 1080p", None),
    ],
)
def test_release_group(name, group):
    assert parse_release(name).group == group


@pytest.mark.xfail(
    reason="BUG: a name ending in 'WEB-DL' gets the release group 'dl', so two "
    "unrelated group-less WEB-DL releases look like the same group to subtitle matching",
    strict=True,
)
def test_web_dl_suffix_is_not_a_release_group():
    from streamfinder.matching import release_match_score

    video = parse_release("Movie.2019.1080p.WEB-DL.mkv")
    assert video.group is None
    other = release_match_score("Movie.2019.720p.WEB-DL", video)
    assert other < 30


@pytest.mark.parametrize(
    "name, service",
    [
        ("Movie.2019.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb", "amzn"),
        ("Movie.2019.1080p.NF.WEBRip.x265", "nf"),
        ("Movie.2019.1080p.DSNP.WEB-DL", "dsnp"),
        ("Movie.2019.1080p.ATVP.WEB", "atvp"),
        ("Movie.2019.1080p.AMZN.BluRay", None),  # a streaming tag only counts on web releases
    ],
)
def test_streaming_service(name, service):
    assert parse_release(name).service == service


def test_editions():
    info = parse_release("Movie.2019.Extended.Directors.Cut.1080p.BluRay")
    assert info.editions == {"extended", "directors"}
    assert parse_release("Interstellar.2014.IMAX.1080p").editions == {"imax"}
    assert parse_release("Movie.2019.1080p.BluRay").editions == set()


# -- cam, sample, junk ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Movie.2019.CAM",
        "Movie.2019.CAMRip.x264",
        "Pacific.Rim.Uprising.2018.HDCAM",
        "Movie.2019.HDTS.x264-X",
        "Movie.2019.TS.720p.x264",
        "Movie.2019.TELESYNC.x264",
        "Movie.2019.HDTC.x264",
        "Movie.2019.DVDSCR.XviD",
        "Movie.2019.SCREENER.x264",
    ],
)
def test_cam_releases(name):
    assert parse_release(name).is_cam


@pytest.mark.parametrize(
    "name",
    [
        "Movie.2019.1080p.WEB-DL.x264.Telesync",  # a real WEB-DL that names its old cam
        "Movie.2019.1080p.BluRay.x264",
        "Movie.2019.1080p.WEBRip.x264",
        "Show.S01E01.720p.HDTV",
    ],
)
def test_not_cam(name):
    assert not parse_release(name).is_cam


@pytest.mark.parametrize(
    "name",
    [
        "Movie.2019.1080p.BluRay.x264-SPARKS.sample.mkv",
        "Movie.2019.1080p.Sample",
        "Movie.2019.1080p.SAMPLE.mkv",
        "Movie/Sample/movie-sample.mkv",
    ],
)
def test_samples(name):
    assert parse_release(name).is_sample


@pytest.mark.parametrize(
    "name",
    [
        "Movie.2019.AI.Upscaled.2160p",
        "Movie.2019.AI-Enhanced.2160p",
        "Narnia.2005.Upscale",
        "Movie.2019.1080p.60fps.WEB",
        "Movie.2019.RiffTrax.1080p",
        "Movie.2019.Commentary.1080p",
        "Movie.2019.Making.Of.1080p",
        "Movie.2019.1080p.Trailer",
        "Movie.2019.Bonus.Features",
        "Movie.2019.Fan.Edit.1080p",
        "Movie.2019.Featurette.720p",
    ],
)
def test_junk_releases(name):
    assert parse_release(name).is_junk


@pytest.mark.parametrize(
    "name",
    [
        "Movie.2019.1080p.BluRay.x264",
        "Show.S01E01.720p.WEB",
        "Movie.2019.24fps.1080p",
        "Movie.2019.Remastered.1080p",
    ],
)
def test_not_junk(name):
    assert not parse_release(name).is_junk


# -- Czech / Slovak versions ---------------------------------------------------------------


@pytest.mark.parametrize(
    "name, kind",
    [
        ("Pelisky.1999.CZ.dabing.1080p", "dub"),
        ("Avatar.The.Way.of.Water.2022.1080p.WEB-DL.CZ.EN", "dub"),
        ("Movie.2019.1080p.CZ.SK.dabing", "dub"),
        ("Movie.2019.Czech.1080p", "dub"),
        ("Shogun.S01.CZ.titulky.1080p", "subs"),
        ("Movie.2019.1080p.BluRay.x264.CZ.titulky", "subs"),
        ("Movie.2019.CZtit.1080p", "subs"),
        ("Movie.2019.CZ.subs.1080p", "subs"),
        ("Movie.2019.1080p.BluRay.x264", None),
        ("Movie 2019 1080p ENG ITA", None),
    ],
)
def test_czech_kind(name, kind):
    assert czech_kind(parse_release(name)) == kind


def test_czech_dub_ranks_above_czech_subtitles():
    dub = czech_bonus(parse_release("Movie.2019.CZ.dabing.1080p"))
    subs = czech_bonus(parse_release("Movie.2019.CZ.titulky.1080p"))
    assert dub > subs > 0
    assert czech_bonus(parse_release("Movie.2019.1080p")) == 0


def test_slovak_only_release_is_slovak():
    info = parse_release("Movie.2019.SK.1080p")
    assert info.has_slovak and not info.has_czech
    assert czech_kind(info) == "dub"
    assert 0 < czech_bonus(info) < czech_bonus(parse_release("Movie.2019.CZ.1080p"))


@pytest.mark.xfail(
    reason="BUG: 'dabing' counts as a Czech token, so a Slovak dub is labelled CZ "
    "and gets the full Czech-dub bonus (22 instead of 14)",
    strict=True,
)
def test_slovak_dub_is_not_labelled_czech():
    info = parse_release("Film.2019.SK.dabing.720p")
    assert info.has_slovak and not info.has_czech
    assert czech_bonus(info) == czech_bonus(parse_release("Film.2019.SK.720p"))


@pytest.mark.xfail(
    reason="BUG: 'SKtit' (Slovak subtitles) is listed as a subtitle token in "
    "torrents/search.py but not as a Slovak token in matching.py, so it is never detected",
    strict=True,
)
def test_slovak_subtitles_tag_is_recognised():
    assert czech_kind(parse_release("Movie.2019.SKtit.1080p")) == "subs"


# -- names with slashes ------------------------------------------------------------------


def test_slash_parts_prefer_the_file_name_of_a_path():
    info = parse_release("Some Folder 2010/Other.Name.2019.1080p.mkv")
    assert info.title == "other name" and info.year == 2019


def test_slash_parts_without_a_year_take_the_longest():
    assert parse_release("CZ / Movie Title 1080p BluRay").title == "movie title"


def test_empty_names_do_not_crash():
    for name in ("", None, "   ", "/", "-"):
        info = parse_release(name)
        assert info.title == "" and info.year is None and not info.episodes


# -- small helpers -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, normalized",
    [
        ("Amélie: Les Fabuleux!", "amelie les fabuleux"),
        ("Ocean’s Eleven", "oceans eleven"),
        ("Fast & Furious", "fast and furious"),
        ("Pokémon: Detective Pikachu", "pokemon detective pikachu"),
        ("Pelíšky", "pelisky"),
        ("Smrť", "smrt"),
        ("  Lots   of   space ", "lots of space"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_title(text, normalized):
    assert normalize_title(text) == normalized


@pytest.mark.parametrize(
    "name, split",
    [
        ("a.MKV", ("a", ".mkv")),
        ("x.srt", ("x", ".srt")),
        ("Movie.2019.1080p.mp4", ("Movie.2019.1080p", ".mp4")),
        ("a.txt", ("a.txt", "")),
        ("Movie.2019", ("Movie.2019", "")),
        ("Movie.1080p.WEB-DL.mkv.part", ("Movie.1080p.WEB-DL.mkv.part", "")),
    ],
)
def test_split_extension(name, split):
    assert split_extension(name) == split


def test_video_and_subtitle_paths():
    assert (
        is_video_path("a/b.M2TS") and is_video_path("x.webm") and is_video_path("x.ts")
    )
    assert not is_video_path("x.nfo") and not is_video_path("x.srt")
    assert all(is_subtitle_path(f"x.{e}") for e in ("srt", "vtt", "ass", "ssa", "sub"))
    assert not is_subtitle_path("x.idx") and not is_subtitle_path("x.mkv")


@pytest.mark.parametrize(
    "value, size",
    [
        ("1.4 GB", int(1.4 * 1024**3)),
        ("700 MB", 700 * 1024**2),
        ("1,5 GiB", int(1.5 * 1024**3)),
        ("2 TB", 2 * 1024**4),
        ("512 KB", 512 * 1024),
        (" 2.0 gb ", 2 * 1024**3),
        ("1503238553", 1503238553),
        (123, 123),
        (1.9, 1),
        ("abc", 0),
        ("", 0),
        (None, 0),
    ],
)
def test_parse_size(value, size):
    assert parse_size(value) == size


@pytest.mark.parametrize(
    "size, text",
    [
        (0, "?"),
        (None, "?"),
        (1, "1 B"),
        (1023, "1023 B"),
        (1024, "1 KB"),
        (5 * 1024**2, "5.00 MB"),
        (int(1.5 * 1024**3), "1.50 GB"),
        (3 * 1024**4, "3.00 TB"),
        (2 * 1024**5, "2048.00 TB"),
    ],
)
def test_format_size(size, text):
    assert format_size(size) == text


def test_size_round_trip_is_close():
    for size in (734_003_200, 1_503_238_553, 25 * 1024**3):
        assert abs(parse_size(format_size(size)) - size) / size < 0.01
