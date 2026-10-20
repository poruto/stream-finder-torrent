from streamfinder.matching import MatchTarget, pack_kind, parse_release, release_match_score, score_torrent, title_similarity


def score(name, target, seeders=50, size=2 * 1024**3):
    return score_torrent(parse_release(name), seeders, size, target)


def test_parse_episode_and_quality():
    info = parse_release("The.Office.US.S02E01E02.720p.WEB-DL.DDP5.1.H.264-NTb.mkv")
    assert info.title == "the office us"
    assert info.season == 2 and info.episodes == [1, 2]
    assert (info.resolution, info.source, info.codec, info.audio, info.group) == (720, "WEB-DL", "H264", "EAC3", "ntb")
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
    from streamfinder.torrents.search import SearchQuery, TorrentSearcher, choose_picks, notice_for

    query = SearchQuery(title="Oppenheimer", titles=["Oppenheimer"], year=2023)
    raw = [
        RawTorrent("Oppenheimer.2023.HDCAM.x264", "a" * 40, 1_500_000_000, 2000, 50, "TPB"),
        RawTorrent("Oppenheimer.2023.1080p.WEBRip.x264.AAC", "b" * 40, 2_800_000_000, 40, 5, "TPB"),
        RawTorrent("Oppenheimer.2023.2160p.BluRay.x265", "c" * 40, 20_000_000_000, 25, 5, "Knaben"),
        RawTorrent("Oppenheimer.2023.720p.WEBRip.x264.AAC", "d" * 40, 1_100_000_000, 30, 5, "YTS"),
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
        RawTorrent("Dune.2021.1080p.WEBRip.x264.AAC", "a" * 40, 2_800_000_000, 400, 10, "YTS"),
        RawTorrent("Dune.2021.2160p.WEB-DL.DDP5.1.HEVC-X", "b" * 40, 15_000_000_000, 60, 5, "TPB"),
        RawTorrent("Dune.2021.720p.WEBRip.x264.AAC", "c" * 40, 1_100_000_000, 90, 5, "YTS"),
    ]
    for quality, expected in [(None, "a"), (1080, "a"), (2160, "b"), (720, "c")]:
        query = SearchQuery(title="Dune", titles=["Dune"], year=2021, quality=quality)
        assert choose_picks(TorrentSearcher._rank(raw, query), quality)["recommended"] == expected * 40, quality


def test_huge_files_are_never_the_default_pick():
    from streamfinder.torrents.providers import RawTorrent
    from streamfinder.torrents.search import SearchQuery, TorrentSearcher, choose_picks

    query = SearchQuery(title="Show", titles=["Show"], media_type="tv", season=1, episode=3,
                        runtime=50, season_episodes={1: 10})
    raw = [
        RawTorrent("Show.S01E03.2160p.WEB-DL.H264.AAC-X", "a" * 40, 50 * 1024**3, 900, 10, "TPB"),  # 50 GB episode
        RawTorrent("Show.S01.1080p.WEB-DL.H264.AAC-X", "b" * 40, 25 * 1024**3, 300, 10, "TPB"),  # 2.5 GB/episode pack
        RawTorrent("Show.S01E03.1080p.WEB-DL.H264.AAC-X", "c" * 40, 2 * 1024**3, 40, 5, "TPB"),
    ]
    ranked = TorrentSearcher._rank(raw, query)
    by_hash = {r.raw.info_hash: r for r in ranked}
    assert by_hash["a" * 40].oversized and not by_hash["b" * 40].oversized
    assert by_hash["b" * 40].per_episode == 25 * 1024**3 // 10
    assert choose_picks(ranked)["recommended"] != "a" * 40
