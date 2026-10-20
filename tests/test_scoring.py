"""Torrent scoring: is this release the wanted film/episode, and how good is it to stream?"""

import pytest

from streamfinder.matching import (
    MatchTarget,
    country_tag,
    parse_release,
    release_match_score,
    same_numbers,
    score_torrent,
    title_similarity,
)

GB = 1024**3


def score(name, target, seeders=50, size=2 * GB, **kwargs):
    return score_torrent(parse_release(name), seeders, size, target, **kwargs)


def movie(title="Dune", year=2021, **kwargs):
    return MatchTarget(titles=[title], media_type="movie", year=year, **kwargs)


def episode(title="Show", season=2, number=3, **kwargs):
    return MatchTarget(
        titles=[title], media_type="tv", season=season, episode=number, **kwargs
    )


# -- title similarity ------------------------------------------------------------------


@pytest.mark.parametrize(
    "candidate, titles, expected",
    [
        ("amelie", ["Amélie"], 1.0),
        ("office", ["The Office"], 1.0),
        ("the office", ["Office"], 1.0),
        ("fast furious", ["Fast & Furious"], 1.0),
        ("fast and furious", ["Fast & Furious"], 1.0),
        ("pelisky", ["Cosy Dens", "Pelíšky"], 1.0),
        ("the office us", ["The Office"], 0.9),
    ],
)
def test_title_similarity_known_values(candidate, titles, expected):
    assert title_similarity(candidate, titles) == expected


def test_title_similarity_empty_inputs():
    assert title_similarity("", ["Dune"]) == 0.0
    assert title_similarity("dune", []) == 0.0
    assert title_similarity("dune", ["", None]) == 0.0


def test_title_similarity_unrelated_titles_are_far_apart():
    assert title_similarity("interstellar", ["Dune"]) < 0.5
    assert title_similarity("dune prophecy", ["Dune"]) < 0.8


def test_title_similarity_is_the_best_over_all_titles():
    one = title_similarity("pelisky", ["Cosy Dens"])
    both = title_similarity("pelisky", ["Cosy Dens", "Pelíšky"])
    assert both == 1.0 > one


# -- sequel numbers and country tags ---------------------------------------------------------


@pytest.mark.parametrize(
    "candidate, titles, same",
    [
        ("rocky iv", ["Rocky IV"], True),
        ("rocky 4", ["Rocky IV"], True),
        ("rocky four", ["Rocky IV"], True),
        ("rocky", ["Rocky IV"], False),
        ("rocky 2", ["Rocky IV"], False),
        ("toy story 3", ["Toy Story 3"], True),
        ("toy story", ["Toy Story 3"], False),
        ("oceans eleven", ["Ocean's Eleven"], True),
        ("scream", ["Scream"], True),
        ("dune part 1", ["Dune: Part One"], True),
        ("dune part two", ["Dune: Part One"], False),
        ("dune part two", ["Dune: Part One", "Dune: Part Two"], True),
    ],
)
def test_same_numbers(candidate, titles, same):
    assert same_numbers(candidate, titles) is same


@pytest.mark.parametrize(
    "candidate, titles, tag",
    [
        ("the office us", ["The Office"], "US"),
        ("the office usa", ["The Office"], "US"),
        ("shameless uk", ["Shameless"], "GB"),
        ("the office", ["The Office"], None),
        ("this is us", ["This Is Us"], None),  # the word belongs to the title
        ("us", ["Us"], None),  # a single word is no tag
        ("wentworth au", ["Wentworth"], "AU"),
    ],
)
def test_country_tag(candidate, titles, tag):
    assert country_tag(candidate, titles) == tag


# -- what gets rejected -----------------------------------------------------------------------


def test_samples_are_always_dropped():
    assert score("Dune.2021.1080p.WEB.sample", movie()) is None
    free = MatchTarget(titles=["x"], free_text=True)
    assert score("Dune.2021.1080p.WEB.sample", free) is None


def test_movie_year_tolerance_is_one_year():
    target = movie()
    assert score("Dune.2020.1080p.WEB", target) is not None
    assert score("Dune.2022.1080p.WEB", target) is not None
    assert score("Dune.2023.1080p.WEB", target) is None
    assert score("Dune.2019.1080p.WEB", target) is None


def test_exact_year_beats_a_year_off():
    target = movie()
    assert score("Dune.2021.1080p.WEB", target) > score("Dune.2020.1080p.WEB", target)
    assert score("Dune.2021.1080p.WEB", target) > score("Dune.1080p.WEB", target)


def test_unrelated_title_is_dropped():
    assert score("Interstellar.2014.1080p.BluRay", movie()) is None
    assert score("Dune.Prophecy.2024.1080p.WEB", movie(year=2024)) is None


def test_movie_rejects_anything_with_episode_markers():
    assert score("Dune.S01.1080p.WEB", movie()) is None
    assert score("Dune.E01.2021.1080p.WEB", movie()) is None


def test_free_text_search_does_not_gate_on_title_or_episode():
    target = MatchTarget(titles=["whatever"], free_text=True)
    assert score("Completely.Different.S01E01.720p", target) is not None
    assert score("Other.1999.1080p", target) is not None


def test_id_match_ignores_naming_differences():
    assert score("Totally.Other.Name.2021.1080p", movie()) is None
    assert score("Totally.Other.Name.2021.1080p", movie(), id_match=True) is not None
    # ...but a movie still is no episode.
    assert score("Totally.Other.Name.S01E01.1080p", movie(), id_match=True) is None


def test_id_mismatch_drops_unless_the_exact_year_is_in_the_name():
    target = movie()
    assert score("Dune.1080p.BluRay", target, id_mismatch=True) is None
    assert score("Dune.2020.1080p.BluRay", target, id_mismatch=True) is None
    tagged = score("Dune.2021.1080p.BluRay", target, id_mismatch=True)
    assert tagged is not None and tagged == score("Dune.2021.1080p.BluRay", target) - 15


def test_sequel_with_the_films_own_year_is_kept_others_dropped():
    assert score("Rocky.IV.1985.1080p", movie("Rocky", 1976)) is None
    assert score("Rocky.1976.1080p", movie("Rocky IV", 1985)) is None
    assert score("Rocky.IV.1985.1080p", movie("Rocky IV", 1985)) is not None
    assert score("Rocky.4.1985.1080p", movie("Rocky IV", 1985)) is not None


def test_ambiguous_title_without_year_needs_the_id():
    target = movie("The Thing", 1982, ambiguous=True)
    assert score("The.Thing.1080p.BluRay", target) is None
    assert score("The.Thing.1080p.BluRay", target, id_match=True) is not None
    assert score("The.Thing.1982.1080p.BluRay", target) is not None


# -- episodes and packs ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, wanted",
    [
        ("Show.S02E03.1080p", True),
        ("Show.2x03.1080p", True),
        ("Show.S02E03E04.1080p", True),
        ("Show.S02E01-E05.1080p", True),
        ("Show.S02.1080p", True),
        ("Show.S01-S03.1080p", True),
        ("Show.Complete.1080p", True),
        ("Show.S02E04.1080p", False),
        ("Show.S03E03.1080p", False),
        ("Show.S03.1080p", False),
        ("Show.S03-S05.1080p", False),
        ("Show.1080p", False),  # neither episode nor pack
        ("Show.E03.1080p", False),  # no season: could be any season
    ],
)
def test_episode_target_accepts_only_what_contains_the_episode(name, wanted):
    assert (score(name, episode(), size=GB) is not None) is wanted


def test_episode_preference_order():
    target = episode()
    exact = score("Show.S02E03.1080p", target, size=GB)
    double = score("Show.S02E03E04.1080p", target, size=GB)
    season = score("Show.S02.1080p", target, size=GB)
    multi = score("Show.S01-S03.1080p", target, size=GB)
    complete = score("Show.Complete.1080p", target, size=GB)
    assert exact > double > season > multi > complete


def test_season_target_prefers_the_season_pack():
    target = MatchTarget(titles=["Show"], media_type="tv", season=2)
    pack = score("Show.S02.1080p", target, size=20 * GB)
    multi = score("Show.S01-S03.1080p", target, size=20 * GB)
    complete = score("Show.Complete.1080p", target, size=20 * GB)
    single = score("Show.S02E03.1080p", target, size=GB)
    assert pack > multi > complete > single
    assert score("Show.S03.1080p", target) is None
    assert score("Show.1080p", target) is None


def test_whole_show_target_gives_packs_a_bonus():
    target = MatchTarget(titles=["Show"], media_type="tv")
    assert score("Show.S02.1080p", target) > score("Show.S02E03.1080p", target)


def test_show_year_without_season_marker_is_a_movie_namesake():
    target = MatchTarget(
        titles=["Show"], media_type="tv", year=2010, season=1, episode=1
    )
    assert score("Show.1995.1080p", target) is None
    assert score("Show.2010.S01E01.720p", target) > score("Show.S01E01.720p", target)
    # A year after the episode marker is the air date, not the show's year.
    assert score("Show.S01E01.1995.720p", target) == score("Show.S01E01.720p", target)


def test_country_tag_needs_the_shows_country():
    us = episode("The Office", 1, 1, countries=["US"])
    assert score("The.Office.UK.S01E01.720p", us) is None
    assert score("The.Office.US.S01E01.720p", us) is not None
    # No country known: a tagged release is fine.
    assert score("The.Office.UK.S01E01.720p", episode("The Office", 1, 1)) is not None
    # Found by IMDb id: the id wins over the tag.
    assert score("The.Office.UK.S01E01.720p", us, id_match=True) is not None


def test_title_word_that_looks_like_a_country_is_not_a_tag():
    target = episode("This Is Us", 1, 1, countries=["US"])
    assert score("This.Is.Us.S01E01.720p", target) is not None


# -- quality preferences ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "preference, best",
    [(None, "1080p"), (1080, "1080p"), (2160, "2160p"), (720, "720p")],
)
def test_preferred_resolution_wins(preference, best):
    target = movie(preferred_resolution=preference)
    scores = {
        r: score(f"Dune.2021.{r}.WEB", target) for r in ("2160p", "1080p", "720p")
    }
    assert max(scores, key=scores.get) == best


def test_unknown_preference_falls_back_to_automatic():
    weird = movie(preferred_resolution=999)
    auto = movie()
    assert score("Dune.2021.1080p.WEB", weird) == score("Dune.2021.1080p.WEB", auto)


def test_720p_fan_is_not_given_a_huge_file():
    target = movie(preferred_resolution=720)
    assert score("Dune.2021.720p.WEB", target, size=3 * GB) < score(
        "Dune.2021.720p.WEB", target, size=2 * GB
    )


def test_size_sanity_for_single_movies_and_episodes():
    target = movie()
    normal = score("Dune.2021.1080p.WEB", target, size=8 * GB)
    assert score("Dune.2021.1080p.WEB", target, size=31 * GB) < normal
    # A 1080p film under 500 MB is a bad encode; a 720p one is merely small.
    assert score("Dune.2021.1080p.WEB", target, size=GB // 3) < normal
    assert score("Dune.2021.720p.WEB", target, size=GB // 3) == score(
        "Dune.2021.720p.WEB", target, size=2 * GB
    )
    tv = episode()
    assert score("Show.S02E03.1080p", tv, size=11 * GB) < score(
        "Show.S02E03.1080p", tv, size=9 * GB
    )


def test_unknown_size_is_not_punished():
    target = movie()
    assert score("Dune.2021.1080p.WEB", target, size=0) == score(
        "Dune.2021.1080p.WEB", target, size=2 * GB
    )


def test_cam_and_junk_are_penalised():
    target = movie()
    clean = score("Dune.2021.1080p.WEB", target)
    assert score("Dune.2021.CAM", target) < clean - 30
    assert score("Dune.2021.1080p.WEB.RiffTrax", target) == clean - 30


def test_browser_friendly_release_beats_silent_audio():
    target = movie()
    aac = score("Dune.2021.1080p.WEBRip.x264.AAC", target)
    ddp = score("Dune.2021.1080p.WEBRip.x264.DDP5.1", target)
    assert aac > ddp


def test_seeders_matter_on_a_log_scale_and_dead_is_worst():
    target = movie()
    dead = score("Dune.2021.1080p.WEB", target, seeders=0)
    one = score("Dune.2021.1080p.WEB", target, seeders=1)
    ten = score("Dune.2021.1080p.WEB", target, seeders=10)
    thousand = score("Dune.2021.1080p.WEB", target, seeders=1000)
    million = score("Dune.2021.1080p.WEB", target, seeders=10**6)
    assert dead < one < ten < thousand
    assert thousand - ten < ten - dead
    assert million == thousand  # capped


def test_scores_are_rounded_to_one_decimal():
    value = score("Dune.2021.1080p.WEBRip.x264.AAC", movie(), seeders=7)
    assert value == round(value, 1)


# -- subtitle <-> video release matching ---------------------------------------------------------


def test_release_match_same_group_and_source_is_best():
    video = parse_release("Movie.2019.1080p.WEB-DL.x264-GRP.mkv")
    same = release_match_score("Movie.2019.1080p.WEB-DL.x264-GRP", video)
    family = release_match_score("Movie.2019.1080p.WEBRip.x264-OTHER", video)
    other = release_match_score("Movie.2019.1080p.HDTV.x264-OTHER", video)
    assert same > family > other


def test_release_match_wrong_episode_or_edition_is_penalised():
    video = parse_release("Show.S01E02.1080p.WEB-DL-GRP")
    assert release_match_score("Show.S01E03.1080p.WEB-DL-GRP", video) < 20
    cut = parse_release("Movie.2019.Extended.1080p.BluRay.x264-GRP")
    assert release_match_score(
        "Movie.2019.1080p.BluRay.x264-GRP", cut
    ) < release_match_score("Movie.2019.Extended.1080p.BluRay.x264-GRP", cut)


def test_release_match_takes_the_best_of_listed_releases():
    video = parse_release("Movie.2019.Extended.1080p.BluRay.x264-GRP")
    listed = "Movie.2019.720p.HDTV-OTHER / Movie.2019.Extended.1080p.BluRay.x264-GRP"
    assert release_match_score(listed, video) == release_match_score(
        "Movie.2019.Extended.1080p.BluRay.x264-GRP", video
    )
    for separator in ("|", ",", ";", "\n"):
        assert release_match_score(listed.replace(" / ", separator), video) > 50


def test_release_match_streaming_service():
    video = parse_release("Movie.2019.1080p.AMZN.WEB-DL-X")
    assert release_match_score(
        "Movie.2019.1080p.AMZN.WEB-DL", video
    ) > release_match_score("Movie.2019.1080p.NF.WEB-DL", video)


def test_release_match_empty_release():
    assert release_match_score("", parse_release("Movie.2019.1080p")) == 0.0
    assert release_match_score(None, parse_release("Movie.2019.1080p")) == 0.0
