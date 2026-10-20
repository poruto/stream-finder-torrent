"""TMDB client (mocked HTTP), presentation helpers and the IMDb ratings table."""

import gzip
import io
import time

import pytest
import requests

from streamfinder import imdb as imdb_module
from streamfinder import tmdb as tmdb_module
from streamfinder.imdb import IMDbRatings
from streamfinder.tmdb import (
    TMDBClient,
    TMDBError,
    image_url,
    match_titles,
    trailer_url,
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise requests.ConnectionError("network disabled in tests")

    monkeypatch.setattr(requests.Session, "send", refuse)


class Answer:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(
                f"{self.status_code} for url https://x/?api_key=secret123&q=1"
            )

    def json(self):
        return self.data


class FakeTMDBHTTP:
    """Routes TMDB paths to canned answers and records every request."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, params=None, timeout=None):
        path = url.removeprefix(TMDBClient.BASE_URL)
        self.calls.append((path, dict(params or {})))
        answer = self.routes.get(path)
        if answer is None:
            return Answer({}, 404)
        if isinstance(answer, Exception):
            raise answer
        return Answer(answer(params) if callable(answer) else answer)


def client_with(routes, language="en-US"):
    client = TMDBClient("key", language)
    client.session = FakeTMDBHTTP(routes)
    return client


# -- requests and the cache -----------------------------------------------------------------


def test_get_sends_the_key_and_language_and_drops_empty_params():
    client = client_with({"/movie/1": {"id": 1}})
    assert client._get("/movie/1", page=2, genre=None) == {"id": 1}
    path, params = client.session.calls[0]
    assert params == {"api_key": "key", "language": "en-US", "page": 2}


def test_answers_are_cached_until_the_ttl_runs_out(monkeypatch):
    client = client_with({"/movie/1": {"id": 1}})
    client._get("/movie/1", ttl=60)
    client._get("/movie/1", ttl=60)
    assert len(client.session.calls) == 1
    client._get("/movie/1", ttl=60, page=2)  # other params: another entry
    assert len(client.session.calls) == 2
    later = time.monotonic() + 61
    monkeypatch.setattr(tmdb_module.time, "monotonic", lambda: later)
    client._get("/movie/1", ttl=60)
    assert len(client.session.calls) == 3


def test_clear_cache_forgets_everything():
    client = client_with({"/movie/1": {"id": 1}})
    client._get("/movie/1")
    client.clear_cache()
    client._get("/movie/1")
    assert len(client.session.calls) == 2


def test_errors_never_leak_the_api_key():
    client = client_with({})
    with pytest.raises(TMDBError) as error:
        client._get("/movie/404")
    assert "secret123" not in str(error.value) and "api_key=***" in str(error.value)
    client.session.routes["/down"] = requests.ConnectionError(
        "https://x/?api_key=secret123 refused"
    )
    with pytest.raises(TMDBError) as error:
        client._get("/down")
    assert "secret123" not in str(error.value)


def test_failed_requests_are_not_cached():
    client = client_with({})
    for _ in range(2):
        with pytest.raises(TMDBError):
            client._get("/movie/404")
    assert len(client.session.calls) == 2


def test_check_key(monkeypatch):
    client = TMDBClient("key")
    monkeypatch.setattr(client.session, "get", lambda *a, **k: Answer({}, 200))
    assert client.check_key("good")
    monkeypatch.setattr(client.session, "get", lambda *a, **k: Answer({}, 401))
    assert not client.check_key("bad")
    monkeypatch.setattr(
        client.session, "get", lambda *a, **k: (_ for _ in ()).throw(requests.Timeout())
    )
    with pytest.raises(TMDBError):
        client.check_key("any")


# -- details -------------------------------------------------------------------------------


def test_details_adds_english_and_czech_titles_and_the_imdb_id():
    def by_language(title_en, title_cs):
        return lambda params: {
            "title": title_en if params["language"] == "en-US" else title_cs,
            "external_ids": {"imdb_id": "tt0137523"},
        }

    client = client_with({"/movie/550": by_language("Fight Club", "Klub rváčů")})
    data = client.details("movie", 550)
    assert data["english_title"] == "Fight Club" and data["czech_title"] == "Klub rváčů"
    assert data["imdb_id"] == "tt0137523"
    main = [p for path, p in client.session.calls if "append_to_response" in p]
    assert len(main) == 1 and "credits" in main[0]["append_to_response"]


def test_details_survives_a_failing_title_lookup(monkeypatch):
    client = client_with({"/tv/1": {"name": "Show", "imdb_id": None}})
    real_get = client._get

    def flaky(path, ttl=3600, language=None, **params):
        if language == "cs-CZ":
            raise TMDBError("down")
        return real_get(path, ttl, language, **params)

    monkeypatch.setattr(client, "_get", flaky)
    data = client.details("tv", 1)
    assert data["english_title"] == "Show" and data["czech_title"] == ""
    assert data["imdb_id"] is None


def test_latest_episode_skips_specials_and_missing_data():
    shows = {
        "/tv/1": {
            "last_episode_to_air": {
                "season_number": 2,
                "episode_number": 5,
                "air_date": "2026-10-01",
            }
        },
        "/tv/2": {"last_episode_to_air": {"season_number": 0, "episode_number": 3}},
        "/tv/3": {"last_episode_to_air": None},
        "/tv/4": {},
    }
    client = client_with(shows)
    assert client.latest_episode(1) == {
        "season": 2,
        "episode": 5,
        "air_date": "2026-10-01",
    }
    assert client.latest_episode(2) is None
    assert client.latest_episode(3) is None
    assert client.latest_episode(4) is None
    # Always asked in English (cached once for every visitor language).
    assert all(p["language"] == "en-US" for _, p in client.session.calls)


def test_namesakes_need_votes_and_a_same_title():
    def item(kind, tmdb_id, title, votes, original=None):
        key = "title" if kind == "movie" else "name"
        return {
            "media_type": kind,
            "id": tmdb_id,
            key: title,
            "vote_count": votes,
            f"original_{key}": original,
        }

    results = [
        item("movie", 1, "The Thing", 5000),
        item("movie", 2, "The Thing", 3000),
        item("tv", 3, "The Office", 4000),
        item("tv", 4, "Kancl", 100, original="The Office"),
        item("movie", 5, "Obscure", 10, original="Interstellar"),
    ]
    client = client_with({"/search/multi": {"results": results}})
    assert client.has_namesake("movie", 1, ["The Thing"])
    assert client.has_namesake("tv", 3, ["The Office"])  # by original title
    assert not client.has_namesake("movie", 6, ["Interstellar"])  # too few votes
    assert not client.has_namesake("movie", 1, ["", None])


def test_namesake_lookup_failure_means_not_ambiguous():
    client = client_with({"/search/multi": requests.Timeout()})
    assert client.has_namesake("movie", 1, ["Dune"]) is False


def test_find_imdb_tags_media_types():
    client = client_with(
        {"/find/tt1": {"movie_results": [{"id": 1}], "tv_results": [{"id": 2}]}}
    )
    assert client.find_imdb("tt1") == [
        {"id": 1, "media_type": "movie"},
        {"id": 2, "media_type": "tv"},
    ]


def test_imdb_ids_skip_failures_and_missing_ids():
    client = client_with(
        {
            "/movie/1/external_ids": {"imdb_id": "tt1"},
            "/tv/2/external_ids": {"imdb_id": None},
        }
    )
    assert client.imdb_ids([("movie", 1), ("tv", 2), ("movie", 3)]) == {
        ("movie", 1): "tt1"
    }


def test_episode_imdb_id_failure_is_none():
    client = client_with({"/tv/1/season/1/episode/2/external_ids": {"imdb_id": "tt9"}})
    assert client.episode_imdb_id(1, 1, 2) == "tt9"
    assert client.episode_imdb_id(1, 1, 3) is None


def test_many_fetches_rows_and_tolerates_a_broken_one():
    client = client_with({"/movie/popular": {"results": [{"id": 1}]}})
    rows = client.many({"popular": ("/movie/popular", {}), "broken": ("/nope", {})})
    assert rows == {"popular": {"results": [{"id": 1}]}, "broken": {"results": []}}


def test_genres_failure_is_an_empty_list():
    client = client_with(
        {"/genre/movie/list": {"genres": [{"id": 28, "name": "Action"}]}}
    )
    assert client.genres("movie") == [{"id": 28, "name": "Action"}]
    assert client.genres("tv") == []


# -- discover filters ----------------------------------------------------------------------


def _discover_params(**kwargs):
    client = client_with({"/discover/movie": {}, "/discover/tv": {}})
    client.discover(**kwargs)
    return client.session.calls[-1][1]


def test_discover_newest_has_an_upper_date_and_needs_votes():
    params = _discover_params(media_type="tv", sort_by="primary_release_date.desc")
    assert params["sort_by"] == "first_air_date.desc"
    assert params["first_air_date.lte"] == time.strftime("%Y-%m-%d")
    assert params["vote_count.gte"] == 20


def test_discover_top_rated_needs_many_votes():
    assert (
        _discover_params(media_type="movie", sort_by="vote_average.desc")[
            "vote_count.gte"
        ]
        == 200
    )
    assert (
        _discover_params(
            media_type="movie", sort_by="vote_average.desc", min_votes=500
        )["vote_count.gte"]
        == 500
    )


def test_discover_decades_and_language():
    params = _discover_params(media_type="movie", decade="1990", language="cs|sk")
    assert params["primary_release_date.gte"] == "1990-01-01"
    assert params["primary_release_date.lte"] == "1999-12-31"
    assert params["with_original_language"] == "cs|sk"
    older = _discover_params(media_type="tv", decade="older")
    assert (
        older["first_air_date.lte"] == "1979-12-31"
        and "first_air_date.gte" not in older
    )
    # The current decade ends today, not in the future.
    current = _discover_params(media_type="movie", decade="2020")
    assert current["primary_release_date.lte"] <= time.strftime("%Y-%m-%d")


def test_discover_year_parameter_per_media_type():
    assert (
        _discover_params(media_type="movie", year=1999)["primary_release_year"] == 1999
    )
    assert _discover_params(media_type="tv", year=1999)["first_air_date_year"] == 1999


# -- helpers -------------------------------------------------------------------------------


def test_image_url():
    assert image_url("/abc.jpg") == "https://image.tmdb.org/t/p/w500/abc.jpg"
    assert image_url("/abc.jpg", "original").endswith("/original/abc.jpg")
    assert image_url(None) is None and image_url("") is None


def test_trailer_prefers_official_youtube_trailers():
    videos = {
        "results": [
            {"site": "Vimeo", "type": "Trailer", "official": True, "key": "vimeo"},
            {"site": "YouTube", "type": "Teaser", "official": True, "key": "teaser"},
            {"site": "YouTube", "type": "Trailer", "official": False, "key": "fan"},
            {
                "site": "YouTube",
                "type": "Trailer",
                "official": True,
                "iso_639_1": "en",
                "key": "en",
            },
            {
                "site": "YouTube",
                "type": "Trailer",
                "official": True,
                "iso_639_1": "cs",
                "key": "cs",
            },
        ]
    }
    assert trailer_url({"videos": videos}) == "https://www.youtube.com/watch?v=cs"
    assert trailer_url({}) is None
    assert trailer_url({"videos": {"results": [videos["results"][0]]}}) is None


def test_match_titles_collects_every_release_name_once():
    data = {
        "english_title": "Cosy Dens",
        "original_title": "Pelíšky",
        "title": "Pelíšky",
        "czech_title": "Pelíšky",
        "alternative_titles": {
            "titles": [
                {"iso_3166_1": "US", "title": "Cozy Dens"},
                {"iso_3166_1": "DE", "title": "Kuschelnester"},
                {"iso_3166_1": "SK", "title": None},
            ]
        },
    }
    assert match_titles(data) == ["Cosy Dens", "Pelíšky", "Cozy Dens"]
    shows = {
        "name": "Show",
        "original_name": "Show",
        "alternative_titles": {"results": [{"iso_3166_1": "GB", "title": "Show UK"}]},
    }
    assert match_titles(shows) == ["Show", "Show UK"]
    assert match_titles({}) == []


# -- IMDb ratings --------------------------------------------------------------------------


class Stream:
    def __init__(self, payload):
        self.raw = io.BytesIO(payload)
        self.raw.decode_content = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass


def _dataset(rows):
    text = "tconst\taverageRating\tnumVotes\n" + "".join(
        f"{t}\t{r}\t{v}\n" for t, r, v in rows
    )
    return gzip.compress(text.encode())


def test_ratings_are_downloaded_and_looked_up(tmp_path, monkeypatch):
    rows = [("tt0111161", 9.3, 3000000), ("tt0068646", 9.2, 2000000)]
    monkeypatch.setattr(
        imdb_module.requests, "get", lambda *a, **k: Stream(_dataset(rows))
    )
    ratings = IMDbRatings(str(tmp_path / "cache" / "ratings.sqlite"))
    assert not ratings.ready and ratings.get("tt0111161") is None and ratings.count == 0
    ratings.refresh()
    assert ratings.ready and ratings.count == 2
    assert ratings.get("tt0111161") == (9.3, 3000000)
    assert ratings.many(["tt0068646", "tt0000001", None, "nonsense", "tt12x"]) == {
        "tt0068646": (9.2, 2000000)
    }
    assert ratings.get(None) is None and ratings.many([]) == {}
    assert [p.name for p in (tmp_path / "cache").iterdir()] == ["ratings.sqlite"]


def test_refresh_replaces_the_table_and_resets_the_count(tmp_path, monkeypatch):
    path = str(tmp_path / "ratings.sqlite")
    payloads = [
        _dataset([("tt1", 5.0, 10)]),
        _dataset([("tt1", 6.0, 20), ("tt2", 7.0, 30)]),
    ]
    monkeypatch.setattr(
        imdb_module.requests, "get", lambda *a, **k: Stream(payloads.pop(0))
    )
    ratings = IMDbRatings(path)
    ratings.refresh()
    assert ratings.count == 1
    ratings.refresh()
    assert ratings.count == 2 and ratings.get("tt1") == (6.0, 20)


def test_failed_download_keeps_the_old_table(tmp_path, monkeypatch):
    path = str(tmp_path / "ratings.sqlite")
    monkeypatch.setattr(
        imdb_module.requests,
        "get",
        lambda *a, **k: Stream(_dataset([("tt1", 5.0, 10)])),
    )
    ratings = IMDbRatings(path)
    ratings.refresh()
    monkeypatch.setattr(
        imdb_module.requests, "get", lambda *a, **k: Stream(b"not gzip")
    )
    with pytest.raises(OSError):
        ratings.refresh()
    assert ratings.get("tt1") == (5.0, 10)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "ratings.sqlite"
    ]  # no temp file left


def test_start_runs_one_background_thread(monkeypatch, tmp_path):
    started = []

    class Thread:
        def __init__(self, target, name, daemon):
            started.append(name)

        def start(self):
            pass

    monkeypatch.setattr(imdb_module.threading, "Thread", Thread)
    ratings = IMDbRatings(str(tmp_path / "r.sqlite"))
    ratings.start()
    ratings.start()
    assert started == ["imdb-ratings"]
