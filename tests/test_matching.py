from streamfinder.matching import (
    MatchTarget,
    pack_kind,
    parse_release,
    release_match_score,
    score_torrent,
    title_similarity,
)


def score(name, target, seeders=50, size=2 * 1024**3):
    return score_torrent(parse_release(name), seeders, size, target)


def test_parse_episode_and_quality():
    info = parse_release("The.Office.US.S02E01E02.720p.WEB-DL.DDP5.1.H.264-NTb.mkv")
    assert info.title == "the office us"
    assert info.season == 2 and info.episodes == [1, 2]
    assert (info.resolution, info.source, info.codec, info.audio, info.group) == (
        720,
        "WEB-DL",
        "H264",
        "EAC3",
        "ntb",
    )
    assert not info.browser_friendly


def test_parse_season_pack_and_ranges():
    assert parse_release("Breaking.Bad.S01.COMPLETE.720p").is_season_pack
    assert parse_release("Friends Season 1-10 Complete").seasons == list(range(1, 11))
    assert parse_release("Show.1x05.HDTV").episodes == [5]


def test_year_and_numeric_titles():
    info = parse_release("2001.A.Space.Odyssey.1968.1080p.BluRay.x264")
    assert info.title == "2001 a space odyssey" and info.year == 1968
    assert parse_release("Blade.Runner.2049.2017.720p").title == "blade runner 2049"


def test_title_similarity_handles_accents_and_articles():
    assert title_similarity("amelie", ["Amélie"]) == 1.0
    assert title_similarity("office", ["The Office"]) == 1.0
    assert title_similarity("dune", ["Interstellar"]) < 0.5


def test_movie_rejects_wrong_year_and_episodes():
    target = MatchTarget(titles=["Dune"], media_type="movie", year=2021)
    assert score("Dune.1984.1080p.BluRay.x264", target) is None
    assert score("Dune.Prophecy.S01E01.1080p", target) is None
    assert score("Dune.2021.1080p.WEBRip.x264.AAC-YTS", target) is not None


def test_episode_prefers_exact_episode_over_pack_and_rejects_others():
    target = MatchTarget(titles=["Breaking Bad"], media_type="tv", season=1, episode=3)
    exact = score("Breaking.Bad.S01E03.720p.WEB-DL.AAC-X", target)
    pack = score("Breaking.Bad.S01.720p.WEB-DL.AAC-X", target)
    assert exact > pack
    assert pack_kind(parse_release("Breaking.Bad.S01.720p.WEB-DL.AAC-X")) == "season"
    assert score("Breaking.Bad.S01E04.720p", target) is None
    assert score("Breaking.Bad.S02.720p", target) is None


def test_cam_and_dead_torrents_rank_low():
    target = MatchTarget(titles=["Oppenheimer"], media_type="movie", year=2023)
    good = score("Oppenheimer.2023.1080p.WEBRip.x264.AAC", target, seeders=40)
    cam = score("Oppenheimer.2023.HDCAM.x264", target, seeders=900)
    dead = score("Oppenheimer.2023.1080p.BluRay.x264", target, seeders=0)
    assert good > cam and good > dead


def test_subtitle_release_matching_prefers_same_group_and_source():
    video = parse_release("Dune.2021.1080p.WEB-DL.DDP5.1.Atmos.H.264-EVO.mkv")
    same = release_match_score("Dune.2021.1080p.WEB-DL.DDP5.1.Atmos.H.264-EVO", video)
    other = release_match_score("Dune.2021.1080p.BluRay.x264-SPARKS", video)
    assert same > other + 30


def test_picks_recommend_real_release_over_popular_cam():
    from streamfinder.torrents.providers import RawTorrent
    from streamfinder.torrents.search import (
        SearchQuery,
        TorrentSearcher,
        choose_picks,
        notice_for,
    )

    query = SearchQuery(title="Oppenheimer", titles=["Oppenheimer"], year=2023)
    raw = [
        RawTorrent(
            "Oppenheimer.2023.HDCAM.x264", "a" * 40, 1_500_000_000, 2000, 50, "TPB"
        ),
        RawTorrent(
            "Oppenheimer.2023.1080p.WEBRip.x264.AAC",
            "b" * 40,
            2_800_000_000,
            40,
            5,
            "TPB",
        ),
        RawTorrent(
            "Oppenheimer.2023.2160p.BluRay.x265",
            "c" * 40,
            20_000_000_000,
            25,
            5,
            "Knaben",
        ),
        RawTorrent(
            "Oppenheimer.2023.720p.WEBRip.x264.AAC",
            "d" * 40,
            1_100_000_000,
            30,
            5,
            "YTS",
        ),
    ]
    ranked = TorrentSearcher._rank(raw, query)
    picks = choose_picks(ranked)
    assert picks["recommended"] == "b" * 40
    assert picks["best_quality"] == "c" * 40
    assert picks["smallest"] == "d" * 40
    assert notice_for(ranked) is None
    cam_only = TorrentSearcher._rank(raw[:1], query)
    assert notice_for(cam_only) == "cam_only"


def test_quality_preference_changes_recommendation():
    from streamfinder.torrents.providers import RawTorrent
    from streamfinder.torrents.search import SearchQuery, TorrentSearcher, choose_picks

    raw = [
        RawTorrent(
            "Dune.2021.1080p.WEBRip.x264.AAC", "a" * 40, 2_800_000_000, 400, 10, "YTS"
        ),
        RawTorrent(
            "Dune.2021.2160p.WEB-DL.DDP5.1.HEVC-X",
            "b" * 40,
            15_000_000_000,
            60,
            5,
            "TPB",
        ),
        RawTorrent(
            "Dune.2021.720p.WEBRip.x264.AAC", "c" * 40, 1_100_000_000, 90, 5, "YTS"
        ),
    ]
    for quality, expected in [(None, "a"), (1080, "a"), (2160, "b"), (720, "c")]:
        query = SearchQuery(title="Dune", titles=["Dune"], year=2021, quality=quality)
        assert (
            choose_picks(TorrentSearcher._rank(raw, query), quality)["recommended"]
            == expected * 40
        ), quality


def test_huge_files_are_never_the_default_pick():
    from streamfinder.torrents.providers import RawTorrent
    from streamfinder.torrents.search import SearchQuery, TorrentSearcher, choose_picks

    query = SearchQuery(
        title="Show",
        titles=["Show"],
        media_type="tv",
        season=1,
        episode=3,
        runtime=50,
        season_episodes={1: 10},
    )
    raw = [
        RawTorrent(
            "Show.S01E03.2160p.WEB-DL.H264.AAC-X",
            "a" * 40,
            50 * 1024**3,
            900,
            10,
            "TPB",
        ),  # 50 GB episode
        RawTorrent(
            "Show.S01.1080p.WEB-DL.H264.AAC-X", "b" * 40, 25 * 1024**3, 300, 10, "TPB"
        ),  # 2.5 GB/episode pack
        RawTorrent(
            "Show.S01E03.1080p.WEB-DL.H264.AAC-X", "c" * 40, 2 * 1024**3, 40, 5, "TPB"
        ),
    ]
    ranked = TorrentSearcher._rank(raw, query)
    by_hash = {r.raw.info_hash: r for r in ranked}
    assert by_hash["a" * 40].oversized and not by_hash["b" * 40].oversized
    assert by_hash["b" * 40].per_episode == 25 * 1024**3 // 10
    assert choose_picks(ranked)["recommended"] != "a" * 40


def test_remakes_are_told_apart_by_year():
    for title, years in [
        ("Dune", (1984, 2021)),
        ("The Thing", (1982, 2011)),
        ("Halloween", (1978, 2007, 2018)),
    ]:
        for year in years:
            target = MatchTarget(titles=[title], year=year, ambiguous=True)
            for other in years:
                name = f"{title.replace(' ', '.')}.{other}.1080p.BluRay.x264-GRP"
                assert (score(name, target) is not None) == (other == year), name
    target = MatchTarget(titles=["The Thing"], year=1982, ambiguous=True)
    assert score("The Thing (1982) 720p BrRip x264 - 650MB - YIFY", target)


def test_name_without_year_needs_an_unambiguous_title():
    name = "The.Thing.1080p.BluRay.x264-GRP"
    remake = MatchTarget(titles=["The Thing"], year=2011, ambiguous=True)
    assert score(name, remake) is None  # the 1982 or the 2011 one?
    # Found by IMDb id: the id settles it.
    assert score_torrent(parse_release(name), 50, 2 * 1024**3, remake, id_match=True)
    # A title no other film has: still fine, just behind a name with the year.
    unique = MatchTarget(titles=["Interstellar"], year=2014)
    assert score("Interstellar.1080p.BluRay.x264-GRP", unique) is not None
    assert score("Interstellar.2014.1080p.BluRay.x264-GRP", unique) > score(
        "Interstellar.1080p.BluRay.x264-GRP", unique
    )


def test_sequel_numbers_are_a_different_film():
    scream = MatchTarget(titles=["Scream"], year=1996)
    assert score("Scream.2.1997.1080p.BluRay.x264", scream) is None
    assert score("Scream.1996.1080p.BluRay.x264", scream) is not None
    sequel = MatchTarget(titles=["Scream 2"], year=1997)
    assert score("Scream.1996.1080p.BluRay.x264", sequel) is None
    assert score("Scream.II.1997.1080p.BluRay.x264", sequel) is not None
    part_one = MatchTarget(titles=["Dune: Part One"], year=2021)
    assert score("Dune.Part.1.2021.1080p.WEB-DL", part_one) is not None
    assert score("Dune.Part.Two.2024.1080p.WEB-DL", part_one) is None


def test_same_name_shows_by_year_and_country():
    def show(title, year, countries=()):
        return MatchTarget(
            titles=[title],
            media_type="tv",
            year=year,
            season=1,
            episode=1,
            ambiguous=True,
            countries=list(countries),
        )

    revival = "Doctor.Who.2005.S01E01.720p.BluRay.x264"
    assert score(revival, show("Doctor Who", 1963)) is None
    assert score(revival, show("Doctor Who", 2005)) > score(
        "Doctor.Who.S01E01.720p.BluRay.x264", show("Doctor Who", 2005)
    )
    us, uk = show("The Office", 2005, ["US"]), show("The Office", 2001, ["GB"])
    assert score("The.Office.UK.S01E01.720p.WEB", us) is None
    assert score("The.Office.US.S01E01.720p.WEB", uk) is None
    assert score("The.Office.US.S01E01.720p.WEB", us) > score(
        "The.Office.S01E01.720p.WEB", us
    )
    assert score("The.Office.S01E01.720p.WEB", uk) is not None


def test_imdb_tag_from_the_index_decides():
    from streamfinder.torrents.providers import RawTorrent, same_imdb
    from streamfinder.torrents.search import SearchQuery, TorrentSearcher

    query = SearchQuery(title="Dune", titles=["Dune"], year=2021, imdb_id="tt1160419")
    size = 2 * 1024**3
    raw = [
        # No year in the name and TPB tagged it as the 1984 film.
        RawTorrent(
            "Dune.1080p.BluRay.x264", "a" * 40, size, 900, 5, "TPB", imdb="tt0087182"
        ),
        # Odd name, right id.
        RawTorrent(
            "Dune.Part.One.1080p.WEB-DL", "b" * 40, size, 50, 5, "TPB", imdb="tt1160419"
        ),
        RawTorrent("Dune.2021.1080p.BluRay.x264-X", "c" * 40, size, 30, 5, "Knaben"),
        # The name's exact year outweighs a (hand-picked) wrong tag, with a penalty.
        RawTorrent(
            "Dune.2021.1080p.BluRay.x264-Y",
            "d" * 40,
            size,
            30,
            5,
            "TPB",
            imdb="0087182",
        ),
    ]
    found = {r.raw.info_hash: r.score for r in TorrentSearcher._rank(raw, query)}
    assert set(found) == {"b" * 40, "c" * 40, "d" * 40}
    assert found["d" * 40] < found["c" * 40]
    assert same_imdb("0386676", "tt0386676") and same_imdb("", "tt1") is None


def test_tmdb_namesakes(monkeypatch):
    from streamfinder.tmdb import TMDBClient

    def movie(tmdb_id, title, votes):
        return {
            "media_type": "movie",
            "id": tmdb_id,
            "title": title,
            "vote_count": votes,
        }

    results = [
        movie(438631, "Dune", 12000),
        movie(841, "Dune", 3000),
        movie(693134, "Dune: Part Two", 6000),
        movie(1, "Dune: Part Two", 3),  # obscure namesake: ignored
        {"media_type": "person", "id": 2, "name": "Dune"},
    ]
    client = TMDBClient("key")
    monkeypatch.setattr(client, "_get", lambda *a, **k: {"results": results})
    assert client.has_namesake("movie", 438631, ["Dune"])
    assert not client.has_namesake("movie", 693134, ["Dune: Part Two"])


def test_slash_in_name_keeps_the_part_with_the_year():
    assert parse_release("Dune Part One (2021) 1080p BluRay EN/ITA").year == 2021
    assert parse_release("Duna / Dune (2021) CZ dabing").title == "dune"
    assert parse_release("Show/Season 1/Show.S01E02.1080p.mkv").episodes == [2]


def test_sequel_number_with_the_films_own_year_is_kept():
    alien3 = MatchTarget(titles=["Alien³"], year=1992, media_type="movie")
    assert score_torrent(parse_release("Alien.3.1992.1080p.BluRay.x264"), 50, 2 * 1024**3, alien3) is not None
    scream = MatchTarget(titles=["Scream"], year=2022, media_type="movie")
    assert score_torrent(parse_release("Scream.5.2022.1080p.WEB-DL"), 50, 2 * 1024**3, scream) is not None
    # Still rejected: a sequel from another year.
    scream_1996 = MatchTarget(titles=["Scream"], year=1996, media_type="movie")
    assert score_torrent(parse_release("Scream.2.1997.1080p.BluRay"), 50, 2 * 1024**3, scream_1996) is None
