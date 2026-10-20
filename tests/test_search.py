"""Torrent search across providers: merging, ranking, picks and resilience (no network)."""

import time

import pytest
import requests

from streamfinder.matching import parse_release
from streamfinder.torrents import search as search_module
from streamfinder.torrents.providers import (
    APIBayProvider,
    EZTVProvider,
    KnabenProvider,
    Provider,
    RawTorrent,
    TorrentsCSVProvider,
    YTSProvider,
    same_imdb,
)
from streamfinder.torrents.search import (
    SearchQuery,
    TorrentResult,
    TorrentSearcher,
    build_magnet,
    choose_picks,
    notice_for,
)

GB = 1024**3


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise requests.ConnectionError("network disabled in tests")

    monkeypatch.setattr(requests.Session, "send", refuse)


def raw(name, letter, size=2 * GB, seeders=50, provider="TPB", **kwargs):
    return RawTorrent(name, letter * 40, size, seeders, 5, provider, **kwargs)


class Response:
    def __init__(self, data=None, status=200, error=None):
        self.data, self.status_code, self.error = data, status, error

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        if self.error:
            raise self.error
        return self.data


class FakeSession:
    """Answers per mirror: a dict of base URL -> Response or exception."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def _answer(self, url, **kwargs):
        self.calls.append((url, kwargs))
        for base, answer in self.answers.items():
            if url.startswith(base):
                if isinstance(answer, Exception):
                    raise answer
                return answer(url, kwargs) if callable(answer) else answer
        raise requests.ConnectionError(url)

    def get(self, url, **kwargs):
        return self._answer(url, **kwargs)

    def post(self, url, **kwargs):
        return self._answer(url, **kwargs)


def provider_with(cls, answers, mirrors=None):
    provider = cls(mirrors or list(answers), timeout=1)
    provider.session = FakeSession(answers)
    return provider


# -- mirrors ----------------------------------------------------------------------------


def test_request_fails_over_to_the_next_mirror_and_promotes_it():
    provider = provider_with(
        Provider,
        {
            "https://a.example": requests.ConnectionError("down"),
            "https://b.example": Response({"ok": 1}),
        },
    )
    assert provider.request("/x") == {"ok": 1}
    assert provider.mirrors == ["https://b.example", "https://a.example"]
    provider.request("/x")
    # The working mirror is asked first now.
    assert provider.session.calls[-1][0] == "https://b.example/x"


@pytest.mark.parametrize(
    "bad",
    [
        Response(status=503),
        Response(error=ValueError("not json")),
        requests.Timeout("slow"),
    ],
)
def test_bad_answers_count_as_a_dead_mirror(bad):
    provider = provider_with(
        Provider, {"https://a.example": bad, "https://b.example": Response([1])}
    )
    assert provider.request("/x") == [1]


def test_no_mirror_answering_is_a_connection_error():
    provider = provider_with(
        Provider,
        {
            "https://a.example": Response(status=500),
            "https://b.example": requests.ConnectionError(),
        },
    )
    with pytest.raises(ConnectionError, match="no mirror answered"):
        provider.request("/x")
    assert not provider.is_available()


def test_trailing_slashes_are_stripped_from_mirrors():
    assert Provider(["https://a.example/"], 1).mirrors == ["https://a.example"]


def test_post_is_used_for_json_bodies():
    provider = provider_with(Provider, {"https://a.example": Response({})})
    provider.request("/v1", json={"q": 1})
    assert provider.session.calls[0][1]["json"] == {"q": 1}


def test_each_query_skips_queries_that_fail():
    def fetch(text):
        if text == "bad":
            raise ConnectionError("down")
        return [raw(text, "a")]

    found = Provider.each_query(["good", "bad", "also"], fetch)
    assert sorted(r.name for r in found) == ["also", "good"]


# -- providers ---------------------------------------------------------------------------


def test_yts_builds_scene_names_from_structured_data():
    data = {
        "data": {
            "movies": [
                {
                    "title_english": "Dune",
                    "year": 2021,
                    "imdb_code": "tt1160419",
                    "torrents": [
                        {
                            "hash": "A" * 40,
                            "quality": "1080p",
                            "type": "bluray",
                            "video_codec": "x264",
                            "size_bytes": 2 * GB,
                            "seeds": 300,
                            "peers": 20,
                        },
                        {
                            "hash": "B" * 40,
                            "quality": "2160p",
                            "type": "web",
                            "video_codec": "x265",
                            "size": "5.1 GB",
                            "seeds": "40",
                            "peers": None,
                        },
                        {"quality": "720p"},  # no hash: skipped
                    ],
                }
            ]
        }
    }
    provider = provider_with(YTSProvider, {"https://yts.example": Response(data)})
    query = SearchQuery(title="Dune", titles=["Dune"], year=2021, imdb_id="tt1160419")
    found = provider.search(query)
    assert [r.info_hash for r in found] == ["a" * 40, "b" * 40]
    assert found[0].name == "Dune 2021 1080p BluRay x264 AAC-YTS"
    assert found[1].name == "Dune 2021 2160p WEBRip x265 AAC-YTS"
    assert found[1].size == int(5.1 * GB) and found[1].seeders == 40
    assert all(r.id_match and r.imdb == "tt1160419" for r in found)
    # Looked up by IMDb id, not by title.
    assert provider.session.calls[0][1]["params"]["query_term"] == "tt1160419"
    info = parse_release(found[0].name)
    assert info.browser_friendly and info.year == 2021


def test_yts_free_text_searches_by_title_and_is_no_id_match():
    provider = provider_with(
        YTSProvider, {"https://yts.example": Response({"data": {}})}
    )
    query = SearchQuery(title="dune", titles=["dune"], imdb_id="tt1", free_text=True)
    assert provider.search(query) == []
    assert provider.session.calls[0][1]["params"]["query_term"] == "dune"


def _eztv_item(season, episode, letter, title=None):
    return {
        "season": str(season),
        "episode": str(episode),
        "hash": letter * 40,
        "title": title or f"Show S{season:02d}E{episode:02d} 720p",
        "size_bytes": str(GB),
        "seeds": 10,
        "peers": 1,
        "imdb_id": "0903747",
    }


def test_eztv_filters_by_season_and_episode_across_pages():
    pages = {
        1: {
            "torrents_count": 250,
            "torrents": [_eztv_item(1, 1, "a"), _eztv_item(2, 3, "b")],
        },
        2: {"torrents": [_eztv_item(2, 3, "c"), _eztv_item(2, 4, "d")]},
        3: {"torrents": [_eztv_item(0, 0, "e", "Show S02 complete")]},
    }
    provider = provider_with(
        EZTVProvider,
        {"https://eztv.example": lambda url, kw: Response(pages[kw["params"]["page"]])},
    )
    query = SearchQuery(
        title="Show",
        titles=["Show"],
        media_type="tv",
        season=2,
        episode=3,
        imdb_id="tt0903747",
    )
    found = provider.search(query)
    assert sorted(r.info_hash[0] for r in found) == ["b", "c", "e"]
    assert {c[1]["params"]["imdb_id"] for c in provider.session.calls} == {"0903747"}
    assert len(provider.session.calls) == 3  # ceil(250 / 100) pages


def test_eztv_missing_page_is_not_fatal():
    def answer(url, kw):
        if kw["params"]["page"] == 2:
            return Response(status=500)
        return Response({"torrents_count": 200, "torrents": [_eztv_item(1, 1, "a")]})

    provider = provider_with(EZTVProvider, {"https://eztv.example": answer})
    query = SearchQuery(title="Show", titles=["Show"], media_type="tv", imdb_id="tt1")
    assert [r.info_hash for r in provider.search(query)] == ["a" * 40]


def test_eztv_needs_an_imdb_id():
    provider = provider_with(EZTVProvider, {"https://eztv.example": Response({})})
    assert (
        provider.search(SearchQuery(title="Show", titles=["Show"], media_type="tv"))
        == []
    )
    assert provider.session.calls == []


def test_apibay_skips_placeholders_and_non_video():
    items = [
        {"info_hash": "0" * 40, "name": "No results returned", "category": "0"},
        {
            "info_hash": "A" * 40,
            "name": "Dune.2021.1080p",
            "category": "207",
            "size": "100",
            "seeders": "9",
            "leechers": "1",
            "imdb": "tt1160419",
        },
        {"info_hash": "B" * 40, "name": "Dune soundtrack", "category": "101"},
        {"info_hash": "", "name": "broken", "category": "201"},
    ]
    provider = provider_with(APIBayProvider, {"https://tpb.example": Response(items)})
    found = provider.search(SearchQuery(title="Dune", titles=["Dune"], year=2021))
    assert [(r.info_hash, r.seeders, r.imdb) for r in found] == [
        ("a" * 40, 9, "tt1160419")
    ]


def test_knaben_sends_categories_except_for_free_text():
    hits = {
        "hits": [
            {
                "hash": "C" * 40,
                "title": "Dune 2021",
                "bytes": 5,
                "liveSeeders": 0,
                "seeders": 7,
                "magnetUrl": "magnet:?x",
            },
            {"title": "no hash"},
        ]
    }
    provider = provider_with(KnabenProvider, {"https://knaben.example": Response(hits)})
    found = provider.search(SearchQuery(title="Dune", titles=["Dune"], media_type="tv"))
    assert found[0].seeders == 7 and found[0].magnet == "magnet:?x"
    assert all(c[1]["json"]["categories"] == [2000000] for c in provider.session.calls)
    provider.session.calls.clear()
    provider.search(SearchQuery(title="dune", titles=["dune"], free_text=True))
    assert "categories" not in provider.session.calls[0][1]["json"]


def test_torrents_csv_lowercases_hashes():
    data = {
        "torrents": [
            {"infohash": "D" * 40, "name": "Dune", "size_bytes": 1, "seeders": 3},
            {"name": "no hash"},
        ]
    }
    provider = provider_with(
        TorrentsCSVProvider, {"https://csv.example": Response(data)}
    )
    found = provider.search(SearchQuery(title="dune", titles=["dune"], free_text=True))
    assert [r.info_hash for r in found] == ["d" * 40]


@pytest.mark.parametrize(
    "a, b, same",
    [
        ("tt0386676", "tt0386676", True),
        ("0386676", "tt0386676", True),
        ("TT386676", "tt0386676", True),
        ("tt1", "tt2", False),
        ("", "tt1", None),
        (None, "tt1", None),
        ("tt1", "nonsense", None),
    ],
)
def test_same_imdb(a, b, same):
    assert same_imdb(a, b) is same


# -- queries -----------------------------------------------------------------------------


def test_text_queries_for_movies_episodes_and_seasons():
    assert SearchQuery(title="Dune", titles=[], year=2021).text_queries() == [
        "Dune 2021"
    ]
    assert SearchQuery(title="Dune", titles=[]).text_queries() == ["Dune"]
    episode = SearchQuery(title="Show", titles=[], media_type="tv", season=1, episode=2)
    assert episode.text_queries() == ["Show S01E02", "Show S01"]
    season = SearchQuery(title="Show", titles=[], media_type="tv", season=3)
    assert season.text_queries() == ["Show S03", "Show Season 3"]
    whole = SearchQuery(title="Show", titles=[], media_type="tv")
    assert whole.text_queries() == ["Show"]
    free = SearchQuery(title="anything goes", titles=[], free_text=True, czech=True)
    assert free.text_queries() == ["anything goes"]


def test_czech_queries_add_local_titles_and_cz_tag_without_duplicates():
    q = SearchQuery(
        title="Friends",
        titles=[],
        media_type="tv",
        season=1,
        episode=2,
        czech=True,
        local_titles=["Pratele", "Friends"],
    )
    assert q.text_queries() == [
        "Friends S01E02",
        "Friends S01",
        "Pratele S01E02",
        "Friends S01E02 CZ",
    ]


def test_cache_key_separates_what_changes_results():
    base = dict(title="Dune", titles=["Dune"], year=2021)
    keys = {
        SearchQuery(**base).cache_key(),
        SearchQuery(**base, quality=720).cache_key(),
        SearchQuery(**base, czech=True).cache_key(),
        SearchQuery(**{**base, "year": 1984}).cache_key(),
        SearchQuery(**base, media_type="tv", season=1).cache_key(),
    }
    assert len(keys) == 5
    assert (
        SearchQuery(**{**base, "title": "DUNE"}).cache_key()
        == SearchQuery(**base).cache_key()
    )


def test_size_per_hour_for_movie_episode_and_packs():
    movie = SearchQuery(title="Dune", titles=["Dune"], runtime=120)
    assert movie.gb_per_hour(parse_release("Dune.2021.1080p"), 4 * GB) == 2.0
    show = SearchQuery(
        title="Show",
        titles=["Show"],
        media_type="tv",
        runtime=30,
        season_episodes={1: 10, 2: 8},
        total_episodes=20,
    )
    assert show.gb_per_hour(parse_release("Show.S01E01"), GB) == 2.0
    assert show.gb_per_hour(parse_release("Show.S01E01E02"), 2 * GB) == 2.0
    assert show.gb_per_hour(parse_release("Show.S01"), 5 * GB) == 1.0
    assert show.gb_per_hour(parse_release("Show.S01-S02"), 9 * GB) == 1.0
    assert show.gb_per_hour(parse_release("Show.Complete"), 10 * GB) == 1.0
    assert show.gb_per_hour(parse_release("Show.S05"), 5 * GB) is None  # unknown season
    assert show.gb_per_hour(parse_release("Show.S01E01"), 0) is None


def test_default_runtime_when_unknown():
    movie = SearchQuery(title="Dune", titles=["Dune"])
    assert movie.gb_per_hour(parse_release("Dune.2021"), 2 * GB) == 1.0  # 120 min
    show = SearchQuery(title="Show", titles=["Show"], media_type="tv")
    assert show.gb_per_hour(parse_release("Show.S01E01"), 3 * GB) == 4.0  # 45 min


# -- ranking -----------------------------------------------------------------------------


def test_duplicates_are_merged_by_hash():
    query = SearchQuery(title="Dune", titles=["Dune"], year=2021, imdb_id="tt1160419")
    found = [
        raw("Dune 2021 1080p", "a", seeders=10, provider="TPB"),
        raw(
            "Dune.2021.1080p.WEB",
            "a",
            seeders=90,
            provider="Knaben",
            magnet="magnet:?xt=k",
        ),
        raw(
            "Dune 2021 1080p BluRay x264 AAC-YTS",
            "a",
            seeders=40,
            provider="YTS",
            id_match=True,
        ),
        raw("Dune 2021 1080p", "a", seeders=5, provider="TPB"),
    ]
    ranked = TorrentSearcher._rank(found, query)
    assert len(ranked) == 1
    result = ranked[0]
    assert result.providers == ["TPB", "Knaben", "YTS"]
    assert result.raw.seeders == 90 and result.raw.magnet == "magnet:?xt=k"
    assert result.raw.id_match
    # YTS names are rebuilt from structured data: they win for parsing.
    assert result.raw.name == "Dune 2021 1080p BluRay x264 AAC-YTS"


def test_ranking_drops_wrong_titles_and_sorts_by_score():
    query = SearchQuery(title="Dune", titles=["Dune"], year=2021)
    found = [
        raw("Dune.2021.720p.WEBRip.x264.AAC", "a", size=GB),
        raw("Interstellar.2014.1080p", "b"),
        raw("Dune.2021.1080p.WEBRip.x264.AAC", "c"),
        raw("Dune.2021.1080p.WEBRip.x264.AAC.sample", "d"),
    ]
    ranked = TorrentSearcher._rank(found, query)
    assert [r.raw.info_hash[0] for r in ranked] == ["c", "a"]
    assert ranked == sorted(ranked, key=lambda r: r.score, reverse=True)


def test_equal_scores_are_ordered_by_seeders():
    query = SearchQuery(title="Dune", titles=["Dune"], year=2021, free_text=False)
    a, b = (
        raw("Dune.2021.1080p", "a", seeders=1000),
        raw("Dune.2021.1080p", "b", seeders=2000),
    )
    ranked = TorrentSearcher._rank([a, b], query)
    assert ranked[0].score == ranked[1].score  # seeder bonus is capped
    assert [r.raw.info_hash[0] for r in ranked] == ["b", "a"]


def test_oversized_files_lose_points():
    query = SearchQuery(title="Dune", titles=["Dune"], year=2021, runtime=120)
    normal = TorrentSearcher._rank(
        [raw("Dune.2021.1080p.BluRay", "a", size=8 * GB)], query
    )[0]
    big = TorrentSearcher._rank(
        [raw("Dune.2021.1080p.BluRay", "b", size=20 * GB)], query
    )[0]
    huge = TorrentSearcher._rank(
        [raw("Dune.2021.1080p.BluRay", "c", size=29 * GB)], query
    )[0]
    assert not normal.oversized and big.oversized and huge.oversized
    assert normal.score - big.score == pytest.approx(12)
    assert normal.score - huge.score == pytest.approx(25)
    # A 4K fan accepts much larger files.
    uhd = SearchQuery(
        title="Dune", titles=["Dune"], year=2021, runtime=120, quality=2160
    )
    assert not TorrentSearcher._rank(
        [raw("Dune.2021.2160p.BluRay", "d", size=29 * GB)], uhd
    )[0].oversized


def test_czech_preference_adds_the_bonus_only_when_asked():
    found = [raw("Shrek.2001.CZ.dabing.1080p", "a")]
    plain = TorrentSearcher._rank(
        found, SearchQuery(title="Shrek", titles=["Shrek"], year=2001)
    )
    czech = TorrentSearcher._rank(
        found, SearchQuery(title="Shrek", titles=["Shrek"], year=2001, czech=True)
    )
    assert czech[0].score - plain[0].score == pytest.approx(22)


def test_free_text_ignores_imdb_tags():
    query = SearchQuery(
        title="dune", titles=["dune"], imdb_id="tt1160419", free_text=True
    )
    found = [raw("Dune.1984.1080p", "a", imdb="tt0087182")]
    assert len(TorrentSearcher._rank(found, query)) == 1


# -- results for the UI ------------------------------------------------------------------


def _result(name, letter="a", seeders=50, size=2 * GB, **kwargs):
    item = raw(name, letter, size=size, seeders=seeders, **kwargs)
    return TorrentResult(item, parse_release(name), 10.0, [item.provider])


@pytest.mark.parametrize(
    "name, quality",
    [
        ("Dune.2021.2160p.WEB", "4K"),
        ("Dune.2021.1080p.WEB", "1080p"),
        ("Dune.2021.720p.WEB", "720p"),
        ("Dune.2021.480p.WEB", "SD"),
        ("Dune.2021.DVDRip", "SD"),
        ("Dune.2021.HDTV", "SD"),
        ("Dune.2021.1080p.HDCAM", "CAM"),
        ("Dune.2021.WEB", None),
    ],
)
def test_quality_label(name, quality):
    assert _result(name).quality == quality


@pytest.mark.parametrize(
    "seeders, availability",
    [
        (500, "excellent"),
        (50, "excellent"),
        (49, "good"),
        (15, "good"),
        (3, "weak"),
        (2, "none"),
        (0, "none"),
    ],
)
def test_availability_label(seeders, availability):
    assert _result("Dune.2021.1080p", seeders=seeders).availability == availability


def test_low_results_are_cam_junk_dead_or_sd():
    assert _result("Dune.2021.CAM").low
    assert _result("Dune.2021.1080p.RiffTrax").low
    assert _result("Dune.2021.1080p", seeders=2).low
    assert _result("Dune.2021.480p").low
    assert not _result("Dune.2021.1080p.WEB").low


def test_to_dict_has_what_the_player_needs():
    result = _result(
        "Show.S01.1080p.WEB-DL.x264.AAC.CZ.SK",
        size=10 * GB,
        provider="SkTorrent",
        title="Show 1. série (CZ)",
    )
    result.per_episode, result.gb_per_hour = GB, 1.23456
    data = result.to_dict()
    assert data["name"] == "Show 1. série (CZ)"  # the original name is shown
    assert data["magnet"].startswith("magnet:?xt=urn:btih:" + "a" * 40)
    assert data["size"] == "10.00 GB" and data["size_bytes"] == 10 * GB
    assert data["pack"] == "season" and data["per_episode"] == "1.00 GB"
    assert data["languages"] == ["CZ", "SK"] and data["czech"] == "dub"
    assert data["source"] == "Web" and data["browser"] is True
    assert data["gb_per_hour"] == 1.23


def test_to_dict_single_file_has_no_per_episode_size():
    result = _result("Dune.2021.1080p.WEB")
    result.per_episode = GB
    assert result.to_dict()["per_episode"] is None


def test_build_magnet_escapes_the_name_and_lists_trackers():
    magnet = build_magnet("a" * 40, "Dune & Co / 2021")
    assert magnet.startswith(
        f"magnet:?xt=urn:btih:{'a' * 40}&dn=Dune%20%26%20Co%20/%202021"
    )
    assert magnet.count("&tr=") == len(search_module.TRACKERS)


# -- picks and notices -------------------------------------------------------------------


def test_no_results_no_picks_no_notice():
    assert choose_picks([]) == {
        "recommended": None,
        "best_quality": None,
        "smallest": None,
    }
    assert notice_for([]) is None


def test_only_dead_torrents_gives_a_low_availability_notice():
    query = SearchQuery(title="Dune", titles=["Dune"], year=2021)
    ranked = TorrentSearcher._rank([raw("Dune.2021.1080p.WEB", "a", seeders=1)], query)
    assert notice_for(ranked) == "low_availability"
    # Still something to play: the best of what there is.
    assert choose_picks(ranked)["recommended"] == "a" * 40


def test_cam_only_notice_when_every_live_torrent_is_a_cam():
    query = SearchQuery(title="Dune", titles=["Dune"], year=2021)
    ranked = TorrentSearcher._rank(
        [
            raw("Dune.2021.HDCAM", "a", seeders=500),
            raw("Dune.2021.1080p.WEB", "b", seeders=1),
        ],
        query,
    )
    assert notice_for(ranked) == "cam_only"


def test_recommendation_prefers_browser_playable_versions():
    query = SearchQuery(title="Dune", titles=["Dune"], year=2021)
    ranked = TorrentSearcher._rank(
        [
            raw("Dune.2021.1080p.BluRay.x264.DTS-GRP", "a", seeders=900),
            raw("Dune.2021.1080p.WEBRip.x264.AAC-YTS", "b", seeders=8),
        ],
        query,
    )
    assert choose_picks(ranked)["recommended"] == "b" * 40


def test_recommendation_falls_back_when_nothing_plays_in_the_browser():
    query = SearchQuery(title="Dune", titles=["Dune"], year=2021)
    ranked = TorrentSearcher._rank(
        [
            raw("Dune.2021.1080p.BluRay.x264.DTS", "a", seeders=900),
            raw("Dune.2021.720p.BluRay.x264.AC3", "b", seeders=20),
        ],
        query,
    )
    assert choose_picks(ranked)["recommended"] == ranked[0].raw.info_hash


def test_smallest_pick_is_much_smaller_and_still_hd():
    query = SearchQuery(title="Dune", titles=["Dune"], year=2021)
    ranked = TorrentSearcher._rank(
        [
            raw("Dune.2021.1080p.WEBRip.x264.AAC", "a", size=3 * GB, seeders=200),
            raw("Dune.2021.720p.WEBRip.x264.AAC", "b", size=GB, seeders=30),
            raw("Dune.2021.480p.WEBRip.x264.AAC", "c", size=GB // 3, seeders=30),
        ],
        query,
    )
    picks = choose_picks(ranked)
    assert picks["recommended"] == "a" * 40 and picks["smallest"] == "b" * 40


def test_no_smallest_pick_when_sizes_are_close():
    query = SearchQuery(title="Dune", titles=["Dune"], year=2021)
    ranked = TorrentSearcher._rank(
        [
            raw("Dune.2021.1080p.WEBRip.x264.AAC", "a", size=3 * GB, seeders=200),
            raw("Dune.2021.720p.WEBRip.x264.AAC", "b", size=int(2.5 * GB), seeders=30),
        ],
        query,
    )
    assert choose_picks(ranked)["smallest"] is None


def test_best_quality_needs_enough_seeders():
    query = SearchQuery(title="Dune", titles=["Dune"], year=2021)
    found = [
        raw("Dune.2021.1080p.WEBRip.x264.AAC", "a", seeders=200),
        raw("Dune.2021.2160p.WEB-DL.x265.DDP", "b", size=12 * GB, seeders=4),
    ]
    assert choose_picks(TorrentSearcher._rank(found, query))["best_quality"] is None
    found[1].seeders = 40
    assert choose_picks(TorrentSearcher._rank(found, query))["best_quality"] == "b" * 40


def test_cam_is_never_recommended_when_a_real_release_exists():
    query = SearchQuery(title="Dune", titles=["Dune"], year=2021)
    ranked = TorrentSearcher._rank(
        [
            raw("Dune.2021.HDCAM.x264", "a", seeders=5000),
            raw("Dune.2021.720p.WEB.x264", "b", seeders=4),
        ],
        query,
    )
    assert choose_picks(ranked)["recommended"] == "b" * 40


# -- the whole search ----------------------------------------------------------------------


class FakeProvider:
    def __init__(
        self, name, results=(), error=None, delay=0.0, media_types=("movie", "tv")
    ):
        self.name, self.results, self.error, self.delay = (
            name,
            list(results),
            error,
            delay,
        )
        self.media_types, self.calls, self.mirrors = (
            media_types,
            0,
            [f"https://{name}.example"],
        )

    def search(self, query):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return [RawTorrent(**vars(r)) for r in self.results]

    def is_available(self):
        return not self.error


@pytest.fixture
def searcher():
    s = TorrentSearcher()
    yield s
    s._pool.shutdown(wait=False, cancel_futures=True)


QUERY = SearchQuery(title="Dune", titles=["Dune"], year=2021)


def test_failing_provider_does_not_break_the_search(searcher):
    searcher.providers = [
        FakeProvider("Good", [raw("Dune.2021.1080p.WEBRip.x264.AAC", "a")]),
        FakeProvider("Broken", error=RuntimeError("HTML instead of JSON")),
        FakeProvider("Down", error=ConnectionError("no mirror answered")),
    ]
    result = searcher.search(QUERY)
    assert [r["hash"] for r in result["results"]] == ["a" * 40]
    assert result["failed_providers"] == ["Broken", "Down"]
    assert result["picks"]["recommended"] == "a" * 40
    assert result["total_found"] == 1


def test_slow_provider_is_skipped_after_the_deadline(searcher, monkeypatch):
    monkeypatch.setattr(search_module, "SEARCH_DEADLINE", 0.2)
    searcher.providers = [
        FakeProvider("Fast", [raw("Dune.2021.1080p.WEB", "a")]),
        FakeProvider("Slow", [raw("Dune.2021.2160p.WEB", "b")], delay=1.0),
    ]
    started = time.monotonic()
    result = searcher.search(QUERY)
    assert time.monotonic() - started < 0.9
    assert result["failed_providers"] == ["Slow"]
    assert [r["hash"] for r in result["results"]] == ["a" * 40]


def test_providers_are_asked_only_for_their_media_type(searcher):
    movies = FakeProvider("Movies", media_types=("movie",))
    shows = FakeProvider("Shows", media_types=("tv",))
    searcher.providers = [movies, shows]
    searcher.search(SearchQuery(title="Show", titles=["Show"], media_type="tv"))
    assert (movies.calls, shows.calls) == (0, 1)


def test_results_are_cached_and_cache_can_be_cleared(searcher):
    provider = FakeProvider("Good", [raw("Dune.2021.1080p.WEB", "a")])
    searcher.providers = [provider]
    first = searcher.search(QUERY)
    second = searcher.search(QUERY)
    assert provider.calls == 1 and second["cached"] and "cached" not in first
    assert second["results"] == first["results"]
    searcher.clear_cache()
    searcher.search(QUERY)
    assert provider.calls == 2


def test_failed_empty_search_is_not_cached(searcher):
    provider = FakeProvider("Down", error=ConnectionError("down"))
    searcher.providers = [provider]
    assert searcher.search(QUERY)["results"] == []
    searcher.search(QUERY)
    assert provider.calls == 2  # tried again: the index may be back


def test_expired_cache_is_searched_again(searcher, monkeypatch):
    provider = FakeProvider("Good", [raw("Dune.2021.1080p.WEB", "a")])
    searcher.providers = [provider]
    searcher.search(QUERY)
    later = time.monotonic() + search_module.CACHE_TTL + 1
    monkeypatch.setattr(search_module.time, "monotonic", lambda: later)
    searcher.search(QUERY)
    assert provider.calls == 2


def test_result_count_is_limited(searcher, monkeypatch):
    monkeypatch.setattr(search_module.config, "MAX_TORRENT_RESULTS", 3)
    letters = "abcdef"
    searcher.providers = [
        FakeProvider(
            "Many",
            [
                raw("Dune.2021.1080p.WEB", c, seeders=10 + i)
                for i, c in enumerate(letters)
            ],
        )
    ]
    result = searcher.search(QUERY)
    assert len(result["results"]) == 3 and result["total_found"] == 6


def test_provider_status_reports_each_provider(searcher):
    searcher.providers = [
        FakeProvider("Up"),
        FakeProvider("Down", error=RuntimeError()),
    ]
    assert searcher.provider_status() == [
        {"name": "Up", "available": True, "url": "https://Up.example"},
        {"name": "Down", "available": False, "url": "https://Down.example"},
    ]


def test_real_providers_fail_cleanly_without_network(searcher):
    # Every provider of a fresh searcher is asked; with no network they all fail, nothing crashes.
    searcher.sktorrent.configure("", "")
    result = searcher.search(
        SearchQuery(title="Dune", titles=["Dune"], year=2021, imdb_id="tt1160419")
    )
    assert result["results"] == []
    assert set(result["failed_providers"]) == {"YTS", "TPB", "Knaben", "Torrents-CSV"}
