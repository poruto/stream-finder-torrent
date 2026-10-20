"""Pages and JSON API through the Flask test client, with TMDB and TorrServer faked."""

import json
import re

import pytest
import requests

from streamfinder import auth, config, create_app, shared, version, views
from streamfinder.tmdb import TMDBError
from streamfinder.torrserver import TorrentFile, TorrServerError

HASH = "08ada5a7a6183aae1e09d831df6748d566095a10"


def poster(tmdb_id, title, media_type="movie", **extra):
    key = "title" if media_type == "movie" else "name"
    date = "release_date" if media_type == "movie" else "first_air_date"
    return {
        "id": tmdb_id,
        key: title,
        "media_type": media_type,
        "poster_path": f"/p{tmdb_id}.jpg",
        "backdrop_path": f"/b{tmdb_id}.jpg",
        "overview": f"About {title}.",
        "vote_average": 7.84,
        "vote_count": 1000,
        "popularity": 50,
        date: "2021-09-15",
        **extra,
    }


MOVIE = {
    "id": 438631,
    "title": "Duna",
    "original_title": "Dune",
    "english_title": "Dune",
    "czech_title": "Duna",
    "release_date": "2021-09-15",
    "runtime": 155,
    "overview": "Paul Atreides...",
    "tagline": "Beyond fear, destiny awaits.",
    "vote_average": 7.8,
    "genres": [{"name": "Sci-Fi"}, {"name": "Adventure"}],
    "poster_path": "/dune.jpg",
    "backdrop_path": "/dune-b.jpg",
    "imdb_id": "tt1160419",
    "credits": {
        "cast": [
            {"name": "Timothée Chalamet", "character": "Paul", "profile_path": "/t.jpg"}
        ]
    },
    "videos": {
        "results": [
            {"site": "YouTube", "type": "Trailer", "official": True, "key": "abc"}
        ]
    },
    "recommendations": {"results": [poster(693134, "Dune: Part Two")]},
    "alternative_titles": {"titles": []},
}

SHOW = {
    "id": 1399,
    "name": "Hra o trůny",
    "original_name": "Game of Thrones",
    "english_title": "Game of Thrones",
    "czech_title": "Hra o trůny",
    "first_air_date": "2011-04-17",
    "episode_run_time": [60],
    "origin_country": ["US"],
    "number_of_episodes": 73,
    "seasons": [
        {"season_number": 0, "name": "Specials", "episode_count": 2},
        {"season_number": 1, "name": "Season 1", "episode_count": 10},
        {"season_number": 2, "name": "", "episode_count": 10},
        {"season_number": 9, "name": "Announced", "episode_count": 0},
    ],
    "alternative_titles": {"results": []},
}


class FakeTMDB:
    """Just enough of TMDBClient for the pages and the API."""

    def __init__(self):
        self.api_key = "test-key"
        self.fail = False
        self.cleared = 0
        self.namesake = False
        self.latest = {}

    def _check(self):
        if self.fail:
            raise TMDBError("TMDB request failed: offline")

    def clear_cache(self):
        self.cleared += 1

    def check_key(self, key):
        return key == "good-key"

    def many(self, rows):
        self._check()
        trending = [
            poster(1, "東京物語"),  # no readable title for the big hero banner
            poster(2, "Dune", overview="Spice."),
            poster(3, "Person", media_type="person"),
        ]
        return {
            name: {
                "results": trending
                if name == "trending"
                else [poster(10 + i, f"{name} {i}") for i in range(3)]
            }
            for name in rows
        }

    def discover(self, media_type, **kwargs):
        self._check()
        self.last_discover = (media_type, kwargs)
        return {"results": [poster(20, "Browse item", media_type)], "total_pages": 3}

    def genres(self, media_type):
        return [{"id": 28, "name": "Action"}]

    def search_multi(self, query, page=1):
        self._check()
        return {
            "results": [
                poster(30, "Something Dune-ish", vote_count=1, popularity=1),
                poster(31, "Dune", release_date="1984-12-14"),
                poster(32, "Dune", release_date="2021-09-15"),
                {"media_type": "person", "name": "Dune Person", "known_for": []},
            ],
            "total_pages": 1,
        }

    def find_imdb(self, imdb_id):
        return [poster(40, "Found by IMDb")]

    def details(self, media_type, tmdb_id):
        self._check()
        return dict(MOVIE if media_type == "movie" else SHOW)

    def season(self, tmdb_id, number):
        return {
            "episodes": [
                {
                    "episode_number": 1,
                    "name": "Winter Is Coming",
                    "air_date": "2011-04-17",
                },
                {"episode_number": 2, "name": ""},
                {"name": "no number"},
            ]
        }

    def has_namesake(self, media_type, tmdb_id, titles):
        return self.namesake

    def latest_episode(self, tmdb_id):
        if tmdb_id == 666:
            raise TMDBError("down")
        return self.latest.get(tmdb_id)

    def imdb_ids(self, items):
        return {}

    def episode_imdb_id(self, tmdb_id, season, episode):
        return "tt0000001"


class FakeProvider:
    name = "Fake"
    media_types = ("movie", "tv")
    mirrors = ["https://fake.example"]

    def search(self, query):
        return []

    def is_available(self):
        return True


@pytest.fixture
def app(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise requests.ConnectionError("network disabled in tests")

    monkeypatch.setattr(requests.Session, "send", refuse)
    for key in config.EDITABLE_KEYS + ("ADMIN_USERNAME", "ADMIN_PASSWORD_HASH"):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(config, key, "")
    monkeypatch.setattr(config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config, "TRANSCRIBE_ENABLED", "false")
    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "settings.env"))
    monkeypatch.setattr(config, "SUBTITLE_CACHE_DIR", str(tmp_path / "subtitles"))
    monkeypatch.setattr(config, "IMDB_DB_PATH", str(tmp_path / "imdb.sqlite"))
    monkeypatch.setattr(config, "TORRSERVER_URL", "http://127.0.0.1:8090")
    monkeypatch.setattr(config, "TORRSERVER_PUBLIC_URL", "")
    monkeypatch.setattr(shared, "PATH", str(tmp_path / "shared.json"))
    monkeypatch.setattr(auth.time, "sleep", lambda _: None)
    auth._failures.clear()
    app = create_app(testing=True)
    svc = app.extensions["streamfinder"]
    svc.tmdb = FakeTMDB()
    svc.torrents.providers = [FakeProvider()]
    monkeypatch.setattr(svc.torrserver, "is_online", lambda: False)
    yield app
    svc.torrents._pool.shutdown(wait=False, cancel_futures=True)
    auth._failures.clear()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def svc(app):
    return app.extensions["streamfinder"]


def login(client):
    return client.post(
        "/api/admin/login", json={"username": "admin", "password": "admin"}
    )


# -- pages -----------------------------------------------------------------------------------


def test_home_shows_rows_and_a_readable_billboard(client):
    response = client.get("/")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Spice." in html  # the billboard is the first title with a Latin name
    assert "popular_movies 0" in html and "Person" not in html


def test_home_in_czech(client):
    client.set_cookie("lang", "cs")
    html = client.get("/").get_data(as_text=True)
    assert 'lang="cs"' in html


@pytest.mark.parametrize("path", ["/movies", "/shows"])
def test_browse_pages_render(client, path):
    response = client.get(path)
    assert response.status_code == 200 and "Browse item" in response.get_data(
        as_text=True
    )


def test_browse_ignores_invalid_filters(client, svc):
    assert (
        client.get("/movies?sort=evil&imdb=3&decade=1850&lang=xx&genre=abc").status_code
        == 200
    )
    media_type, kwargs = svc.tmdb.last_discover
    assert media_type == "movie" and kwargs["sort_by"] == "popularity.desc"
    assert (
        kwargs["decade"] is None
        and kwargs["language"] is None
        and kwargs["genre"] is None
    )


def test_browse_passes_valid_filters_on(client, svc):
    client.get("/shows?sort=new&decade=1990&lang=cs&genre=28")
    media_type, kwargs = svc.tmdb.last_discover
    assert media_type == "tv" and kwargs["genre"] == 28 and kwargs["decade"] == "1990"
    assert (
        kwargs["language"] == "cs|sk"
        and kwargs["sort_by"] == "primary_release_date.desc"
    )


def test_search_page_ranks_by_year_and_exact_title(client):
    html = client.get("/search?q=Dune+2021").get_data(as_text=True)
    assert html.index("/movie/32") < html.index("/movie/30") < html.index("/movie/31")


def test_search_page_without_a_query(client):
    assert client.get("/search").status_code == 200
    assert client.get("/search?q=%20%20").status_code == 200


def test_search_by_imdb_link(client):
    html = client.get("/search?q=https://www.imdb.com/title/tt1160419/").get_data(
        as_text=True
    )
    assert "Found by IMDb" in html


def test_movie_detail_page(client):
    response = client.get("/movie/438631")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "Duna" in html and "Timothée Chalamet" in html
    assert "https://www.youtube.com/watch?v=abc" in html
    assert "https://www.imdb.com/title/tt1160419/" in html
    assert "csfd.cz/hledat/?q=Dune+2021" in html


def test_show_detail_page_with_seasons(client):
    html = client.get("/show/1399").get_data(as_text=True)
    assert "Winter Is Coming" in html
    # Specials go last, empty seasons are hidden.
    assert html.index("Season 1") < html.index("Specials") and "Announced" not in html
    assert client.get("/show/1399?season=2").status_code == 200
    assert (
        client.get("/show/1399?season=77").status_code == 200
    )  # unknown: first season


def test_settings_and_status_pages(client):
    assert client.get("/settings").status_code == 200
    html = client.get("/status").get_data(as_text=True)
    assert (
        'id="health"' in html and "js/status.js" in html
    )  # the checks come from /api/health


def test_pages_need_a_tmdb_key_first(client, monkeypatch):
    monkeypatch.setattr(config, "TMDB_API_KEY", "")
    for path in ("/", "/movies", "/shows", "/search?q=x", "/movie/1", "/show/1"):
        response = client.get(path)
        assert response.status_code == 302, path
        assert response.headers["Location"].endswith("/settings?setup=1")
    assert client.get("/settings?setup=1").status_code == 200
    assert client.get("/status").status_code == 200
    assert client.get("/lang/cs").status_code == 302
    assert client.get("/api/healthz").status_code == 200


def test_unknown_page_is_a_html_404(client):
    response = client.get("/no/such/page")
    assert response.status_code == 404 and response.mimetype == "text/html"


def test_tmdb_down_is_a_502_page_and_json(client, svc):
    svc.tmdb.fail = True
    page = client.get("/movies")
    assert page.status_code == 502 and page.mimetype == "text/html"
    api = client.get("/api/browse?type=movie")
    assert api.status_code == 502 and api.get_json()["success"] is False


def test_static_files_are_cached_for_a_year_when_versioned(client):
    assert client.get("/static/css/app.css?v=123").cache_control.max_age == 31536000
    assert client.get("/static/css/app.css").cache_control.max_age == 300


def test_service_worker_is_served_from_the_root_with_this_version(client, monkeypatch):
    monkeypatch.setattr(config, "TMDB_API_KEY", "")  # needed before the setup too
    response = client.get("/sw.js")
    assert response.status_code == 200 and response.mimetype == "text/javascript"
    assert response.headers["Service-Worker-Allowed"] == "/"
    assert response.cache_control.no_cache  # a new version is noticed at once
    script = response.get_data(as_text=True)
    assert f"const VERSION = {json.dumps(version.VERSION)};" in script
    shell = json.loads(re.search(r"const SHELL = (\[.*?\]);", script).group(1))
    # The app shell, as the pages link it: CSS and scripts with ?v=, icons without.
    assert any(re.fullmatch(r"/static/css/app\.css\?v=\d+", url) for url in shell)
    for name in ("app", "profiles", "player/core", "player/page", "calendar"):
        assert any(url.startswith(f"/static/js/{name}.js?v=") for url in shell), name
    assert "/static/manifest.json" in shell
    assert "/static/icons/icon-192.png" in shell
    assert not [url for url in shell if "/api/" in url or url.endswith(".vtt")]


def test_pages_register_the_service_worker_only_in_a_secure_context(client):
    html = client.get("/").get_data(as_text=True)
    assert 'navigator.serviceWorker.register("/sw.js")' in html
    assert '"serviceWorker" in navigator && window.isSecureContext' in html


def test_poster_srcset_offers_three_tmdb_sizes():
    base = "https://image.tmdb.org/t/p"
    assert views.poster_srcset(f"{base}/w500/p.jpg") == (
        f"{base}/w185/p.jpg 185w, {base}/w342/p.jpg 342w, {base}/w500/p.jpg 500w"
    )
    assert views.poster_srcset(None) is None
    assert views.poster_srcset("/static/icons/icon-192.png") is None


def test_posters_in_rows_and_grids_have_a_srcset(client):
    for path in ("/", "/movies"):
        html = client.get(path).get_data(as_text=True)
        images = re.findall(r'<a class="poster[^"]*"[^>]*>\s*<img [^>]*>', html)
        assert images, path
        for image in images:
            poster = re.search(r'src="([^"]+)"', image).group(1)
            assert f'srcset="{views.poster_srcset(poster)}"' in image, image
            assert 'sizes="(max-width: 760px) 31vw, ' in image, image


# -- language switch and old URLs --------------------------------------------------------------


def test_language_switch_sets_the_cookie_and_goes_back(client):
    response = client.get("/lang/cs?next=/movies?genre=28")
    assert (
        response.status_code == 302
        and response.headers["Location"] == "/movies?genre=28"
    )
    assert "lang=cs" in response.headers["Set-Cookie"]
    assert client.get("/lang/xx").status_code == 404


@pytest.mark.parametrize(
    "target", ["//evil.example/", "https://evil.example/x", "http://evil.example"]
)
def test_language_switch_is_no_open_redirect(client, target):
    response = client.get("/lang/en", query_string={"next": target})
    assert response.headers["Location"] == "/"


def test_language_switch_backslash_stays_on_this_site(client):
    response = client.get("/lang/en", query_string={"next": "/\\evil.example"})
    assert response.headers["Location"] == "/"  # browsers treat /\ like //


@pytest.mark.parametrize(
    "target", ["https:evil.example", "http:/evil.example", "javascript:alert(1)"]
)
def test_language_switch_rejects_scheme_only_redirects(client, target):
    response = client.get("/lang/en", query_string={"next": target})
    assert response.headers["Location"] == "/"


@pytest.mark.parametrize(
    "old, new",
    [
        ("/filmy", "/movies"),
        ("/serialy?genre=18", "/shows?genre=18"),
        ("/hledat?q=dune", "/search?q=dune"),
        ("/nastaveni", "/settings"),
        ("/stav", "/status"),
        ("/film/5?play=1", "/movie/5?play=1"),
        ("/serial/7", "/show/7"),
        ("/title/movie/3", "/movie/3"),
        ("/title/tv/3", "/show/3"),
        ("/season/1/2", "/show/1?season=2"),
        ("/episode/1/2/3", "/show/1?season=2#e3"),
        ("/episode-torrents?tmdb_id=4&season=1", "/show/4?season=1"),
    ],
)
def test_old_urls_redirect_permanently(client, old, new):
    response = client.get(old)
    assert response.status_code == 301 and response.headers["Location"] == new


def test_old_urls_with_bad_parts_are_404(client):
    assert client.get("/title/person/3").status_code == 404
    assert client.get("/episode-torrents?tmdb_id=4").status_code == 404


# -- JSON API ----------------------------------------------------------------------------------


def test_healthz(client):
    response = client.get("/api/healthz")
    assert response.status_code == 200 and response.get_data(as_text=True) == "ok"


def test_unknown_api_path_is_a_json_404(client):
    response = client.get("/api/does-not-exist")
    assert response.status_code == 404 and response.get_json() == {
        "success": False,
        "error": response.get_json()["error"],
    }


def test_api_browse_search_and_season(client):
    data = client.get("/api/browse?type=tv&page=999").get_json()
    assert data["items"][0]["type"] == "tv" and data["next_page"] is None
    assert client.get("/api/search?q=").get_json() == {"items": []}
    items = client.get("/api/search?q=dune&type=tv").get_json()["items"]
    assert items == []  # every result is a movie
    episodes = client.get("/api/season/1399/1").get_json()["episodes"]
    assert [e["episode"] for e in episodes] == [1, 2]
    assert episodes[1]["name"]  # a fallback name "Episode 2"


@pytest.mark.parametrize(
    "path",
    [
        "/api/torrents",
        "/api/play",
        "/api/subtitles/find",
        "/api/admin/login",
        "/api/keys",
    ],
)
def test_post_endpoints_want_a_json_object(client, path):
    login(client)
    for body in ({"data": "not json"}, {"json": [1, 2]}):
        response = client.post(path, **body)
        assert response.status_code == 400, (path, body)
        assert response.get_json()["success"] is False


def test_torrents_need_a_title_or_a_query(client):
    response = client.post("/api/torrents", json={})
    assert response.status_code == 400


def test_torrent_search_query_is_built_from_tmdb(client, svc, monkeypatch):
    seen = []
    monkeypatch.setattr(
        svc.torrents, "search", lambda q: seen.append(q) or {"results": []}
    )
    svc.tmdb.namesake = True
    body = {"tmdb_id": 438631, "media_type": "movie", "quality": 1080, "czech": True}
    assert client.post("/api/torrents", json=body).get_json()["success"]
    query = seen[0]
    assert query.title == "Dune" and query.year == 2021 and query.imdb_id == "tt1160419"
    assert "Dune" in query.titles and "Duna" in query.titles
    assert (
        query.ambiguous
        and query.czech
        and query.quality == 1080
        and query.runtime == 155
    )
    assert query.local_titles == ["Duna"]


def test_torrent_search_for_an_episode(client, svc, monkeypatch):
    seen = []
    monkeypatch.setattr(
        svc.torrents, "search", lambda q: seen.append(q) or {"results": []}
    )
    client.post(
        "/api/torrents",
        json={
            "tmdb_id": 1399,
            "media_type": "tv",
            "season": "1",
            "episode": 2,
            "quality": 480,
        },
    )
    query = seen[0]
    assert (query.media_type, query.season, query.episode) == ("tv", 1, 2)
    assert (
        query.season_episodes == {0: 2, 1: 10, 2: 10, 9: 0}
        and query.total_episodes == 73
    )
    assert query.countries == ["US"] and query.runtime == 60
    assert query.quality is None  # 480 is no supported preference
    assert query.local_titles == [
        "Hra o truny"
    ]  # without diacritics, like release names


def test_free_text_torrent_search(client, svc, monkeypatch):
    seen = []
    monkeypatch.setattr(
        svc.torrents, "search", lambda q: seen.append(q) or {"results": []}
    )
    client.post("/api/torrents", json={"query": "  some release 1080p "})
    assert seen[0].free_text and seen[0].title == "some release 1080p"


def test_play_refuses_bad_magnets(client):
    for magnet in ("", "magnet:?xt=urn:btih:nothex", "http://example.com/x.torrent"):
        assert client.post("/api/play", json={"magnet": magnet}).status_code == 400


def test_play_picks_the_episode_and_a_browser_reachable_url(client, svc, monkeypatch):
    files = [
        TorrentFile(0, "Show.S01/Show.S01E01.mkv", 900_000_000),
        TorrentFile(1, "Show.S01/Show.S01E02.mkv", 800_000_000),
        TorrentFile(2, "Show.S01/Show.S01E02.en.srt", 40_000),
    ]
    monkeypatch.setattr(svc.torrserver, "add", lambda magnet, title="", poster="": HASH)
    monkeypatch.setattr(svc.torrserver, "files", lambda info_hash, wait=0: files)
    response = client.post(
        "/api/play",
        json={"magnet": HASH.upper(), "season": 1, "episode": 2},
        base_url="http://192.168.1.20:5000",
    )
    data = response.get_json()
    assert data["success"] and data["file"]["index"] == 1
    assert data["stream_url"].startswith(
        "http://192.168.1.20:8090/stream/Show.S01E02.mkv?link="
    )
    assert [f["index"] for f in data["files"]] == [0, 1]


def test_play_reports_torrserver_offline(client, svc, monkeypatch):
    def offline(*args, **kwargs):
        raise TorrServerError("TorrServer offline", "offline")

    monkeypatch.setattr(svc.torrserver, "add", offline)
    response = client.post("/api/play", json={"magnet": HASH})
    assert response.status_code == 502 and response.get_json()["error"]


def test_play_without_files_asks_to_retry(client, svc, monkeypatch):
    monkeypatch.setattr(svc.torrserver, "add", lambda *a, **k: HASH)
    monkeypatch.setattr(svc.torrserver, "files", lambda *a, **k: [])
    response = client.post("/api/play", json={"magnet": HASH})
    assert response.status_code == 504 and response.get_json()["retry"] is True


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/api/torrent-stats/nothex"),
        ("post", "/api/torrent-stop/nothex"),
        ("get", "/api/probe/nothex/0"),
        ("get", "/api/subtitles/ai/nothex/0"),
    ],
)
def test_bad_info_hashes_are_404_or_refused(client, svc, monkeypatch, method, path):
    monkeypatch.setattr(svc.transcoder, "ffmpeg", "ffmpeg", raising=False)
    response = getattr(client, method)(path)
    assert response.status_code in (404, 501)


def test_torrent_stats(client, svc, monkeypatch):
    monkeypatch.setattr(
        svc.torrserver,
        "get",
        lambda h: {"connected_seeders": 4, "active_peers": 9, "download_speed": 1234.5},
    )
    assert client.get(f"/api/torrent-stats/{HASH}").get_json() == {
        "success": True,
        "seeders": 4,
        "peers": 9,
        "download_speed": 1234,
    }
    monkeypatch.setattr(svc.torrserver, "get", lambda h: None)  # unloaded torrent
    assert client.get(f"/api/torrent-stats/{HASH}").get_json()["seeders"] == 0


def test_subtitle_search_needs_a_hash(client):
    response = client.post("/api/subtitles/find", json={"hash": "123"})
    assert response.status_code == 400


def test_subtitle_file_with_a_bad_id_is_404(client):
    response = client.get("/api/subtitles/whatever.vtt")
    assert response.status_code == 404 and response.mimetype == "text/plain"


def test_subtitle_file_is_served_as_webvtt(client, svc, monkeypatch):
    monkeypatch.setattr(
        svc.subtitles,
        "get_vtt",
        lambda cid: "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHi\n",
    )
    response = client.get("/api/subtitles/os-1.vtt")
    assert response.status_code == 200 and response.mimetype == "text/vtt"
    assert "max-age=86400" in response.headers["Cache-Control"]


def test_subtitle_download_failure_is_502(client, svc, monkeypatch):
    def quota(cid):
        raise RuntimeError("daily limit")

    monkeypatch.setattr(svc.subtitles, "get_vtt", quota)
    assert client.get("/api/subtitles/os-1.vtt").status_code == 502


def test_ai_subtitles_when_switched_off(client):
    response = client.get(f"/api/subtitles/ai/{HASH}/0")
    assert response.status_code == 501 and response.get_json()["success"] is False


# -- shared progress and new episodes ---------------------------------------------------------


def test_shared_state_starts_empty(client):
    assert client.get("/api/shared").get_json() == {
        "success": True,
        "progress": {},
        "list": [],
        "listRemoved": {},
    }


def test_shared_list_is_newest_first_and_survives_garbage(client):
    client.post(
        "/api/shared",
        json={"list": [{"key": "a", "added": 1}, {"key": "b", "added": 5}, "junk"]},
    )
    assert [i["key"] for i in client.get("/api/shared").get_json()["list"]] == [
        "b",
        "a",
    ]
    response = client.post(
        "/api/shared", data="not json at all", content_type="text/plain"
    )
    assert response.status_code == 200 and len(response.get_json()["list"]) == 2


@pytest.mark.parametrize("body", [[1, 2], {"progress": ["x"]}])
def test_shared_state_ignores_malformed_bodies(client, body):
    assert client.post("/api/shared", json=body).status_code == 200


def test_new_episodes_skip_unknown_failed_and_bad_ids(client, svc):
    svc.tmdb.latest = {1: {"season": 2, "episode": 5, "air_date": "2026-10-01"}}
    data = client.get("/api/new-episodes?ids=1,2,666,abc,-3").get_json()
    assert data == {
        "success": True,
        "latest": {"1": {"season": 2, "episode": 5, "air_date": "2026-10-01"}},
    }
    assert client.get("/api/new-episodes").get_json() == {"success": True, "latest": {}}


def test_new_episodes_asks_at_most_40_shows(client, svc):
    asked = []
    svc.tmdb.latest_episode = lambda tmdb_id: asked.append(tmdb_id)
    client.get("/api/new-episodes?ids=" + ",".join(str(i) for i in range(100)))
    assert sorted(asked) == list(range(40))


# -- admin-only settings ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method, path",
    [("get", "/api/keys"), ("post", "/api/keys"), ("post", "/api/admin/credentials")],
)
def test_settings_endpoints_need_the_admin(client, method, path):
    response = getattr(client, method)(path, json={"tmdb_api_key": "good-key"})
    assert response.status_code == 401 and response.get_json()["success"] is False


def test_logout_ends_the_admin_session(client):
    login(client)
    assert client.get("/api/keys").status_code == 200
    client.post("/api/admin/logout")
    assert client.get("/api/keys").status_code == 401


def test_key_status_never_contains_the_keys(client, monkeypatch):
    monkeypatch.setattr(config, "OPENSUBTITLES_API_KEY", "os-secret-key")
    login(client)
    data = client.get("/api/keys").get_json()
    assert data["tmdb"] is True and data["opensubtitles"] is True
    assert "os-secret-key" not in str(data) and "test-key" not in str(data)


def test_invalid_tmdb_key_is_not_saved(client, tmp_path):
    login(client)
    response = client.post("/api/keys", json={"tmdb_api_key": "bad-key"})
    assert response.status_code == 400 and response.get_json()["field"] == "tmdb"
    assert not (tmp_path / "settings.env").exists()


def test_valid_tmdb_key_is_saved_and_used_at_once(client, svc, tmp_path):
    login(client)
    response = client.post("/api/keys", json={"tmdb_api_key": "  good-key  "})
    assert response.status_code == 200 and response.get_json()["success"]
    assert "TMDB_API_KEY=good-key" in (tmp_path / "settings.env").read_text(
        encoding="utf-8"
    )
    assert config.TMDB_API_KEY == "good-key" and svc.tmdb.api_key == "good-key"
    assert svc.tmdb.cleared == 1


def test_tmdb_unreachable_while_checking_the_key(client, svc, monkeypatch):
    def down(key):
        raise TMDBError("offline")

    monkeypatch.setattr(svc.tmdb, "check_key", down)
    login(client)
    assert client.post("/api/keys", json={"tmdb_api_key": "x"}).status_code == 502


def test_whisper_address_must_be_a_url(client):
    login(client)
    response = client.post("/api/keys", json={"transcribe_api_url": "192.168.0.5:8178"})
    assert response.status_code == 400 and response.get_json()["field"] == "transcribe"


def test_api_provider_needs_a_key(client):
    login(client)
    response = client.post("/api/keys", json={"transcribe_provider": "api"})
    assert response.status_code == 400


def test_removing_logins_clears_them(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SKTORRENT_USERNAME", "user")
    monkeypatch.setattr(config, "SKTORRENT_PASSWORD", "pass")
    monkeypatch.setattr(config, "OPENSUBTITLES_API_KEY", "key")
    login(client)
    data = client.post(
        "/api/keys", json={"remove": ["sktorrent", "opensubtitles"]}
    ).get_json()
    assert data["sktorrent"] is False and data["opensubtitles"] is False
    text = (tmp_path / "settings.env").read_text(encoding="utf-8")
    assert "SKTORRENT_PASSWORD=\n" in text and "OPENSUBTITLES_API_KEY=\n" in text


def test_nothing_to_change_writes_nothing(client, tmp_path):
    login(client)
    assert client.post(
        "/api/keys", json={"unknown": "x", "tmdb_api_key": 5}
    ).get_json()["success"]
    assert not (tmp_path / "settings.env").exists()


def test_credentials_change_needs_the_current_password(client):
    login(client)
    response = client.post(
        "/api/admin/credentials",
        json={"current_password": "wrong", "new_password": "long-enough-1"},
    )
    assert response.status_code == 400


def test_login_answer_tells_about_the_default_password(client):
    assert login(client).get_json() == {"success": True, "default_password": True}
    response = client.post(
        "/api/admin/login", json={"username": "admin", "password": "nope"}
    )
    assert response.status_code == 401 and response.get_json()["error"]
