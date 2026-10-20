"""Up next, because you watched, collections and mark as watched."""

import pytest

from streamfinder import create_app, shared
from streamfinder.services import services
from streamfinder.shared import merge, next_episode

TODAY = "2026-10-20"


class FakeTMDB:
    """Just the summaries next_episode() reads; counts season lookups."""

    def __init__(self, show, seasons=None):
        self.show = show
        self.seasons = seasons or {}
        self.season_calls = []

    def show_summary(self, tmdb_id):
        return self.show

    def season_summary(self, tmdb_id, season_number):
        self.season_calls.append(season_number)
        return {"episodes": self.seasons.get(season_number, [])}


def season(number, count, air_date=None):
    return {"season_number": number, "episode_count": count, "air_date": air_date}


def ep(season_number, episode_number, air_date=None):
    return {
        "season_number": season_number,
        "episode_number": episode_number,
        "air_date": air_date,
    }


SHOW = {
    # Specials come first in TMDB's list and must never be "next".
    "seasons": [season(0, 4), season(1, 10), season(2, 8)],
    "last_episode_to_air": ep(2, 8, "2025-05-01"),
    "next_episode_to_air": None,
}


def test_next_episode_in_the_same_season():
    tmdb = FakeTMDB(SHOW)
    assert next_episode(tmdb, 1, 1, 3, TODAY) == {
        "season": 1,
        "episode": 4,
        "aired": True,
        "air_date": None,
    }
    assert tmdb.season_calls == []  # the cached summary was enough


def test_last_episode_of_a_season_rolls_over_skipping_specials():
    result = next_episode(FakeTMDB(SHOW), 1, 1, 10, TODAY)
    assert (result["season"], result["episode"], result["aired"]) == (2, 1, True)


def test_end_of_the_show_and_specials_have_no_next():
    assert next_episode(FakeTMDB(SHOW), 1, 2, 8, TODAY) is None
    assert next_episode(FakeTMDB(SHOW), 1, 0, 2, TODAY) is None


def test_season_rollover_skips_a_gap_in_numbering():
    show = {
        **SHOW,
        "seasons": [season(0, 2), season(1, 6), season(3, 6)],
        "last_episode_to_air": ep(3, 6),
    }
    result = next_episode(FakeTMDB(show), 1, 1, 6, TODAY)
    assert (result["season"], result["episode"]) == (3, 1)


def test_unaired_next_episode_carries_its_announced_date():
    show = {
        "seasons": [season(1, 8), season(2, 8, "2026-11-12")],
        "last_episode_to_air": ep(1, 8, "2025-03-01"),
        "next_episode_to_air": ep(2, 1, "2026-11-12"),
    }
    tmdb = FakeTMDB(show)
    assert next_episode(tmdb, 1, 1, 8, TODAY) == {
        "season": 2,
        "episode": 1,
        "aired": False,
        "air_date": "2026-11-12",
    }
    assert tmdb.season_calls == []


def test_unaired_episode_later_in_a_season_looks_up_its_date():
    show = {
        "seasons": [season(1, 10)],
        "last_episode_to_air": ep(1, 5, "2026-10-14"),
        "next_episode_to_air": ep(1, 6, "2026-10-21"),
    }
    tmdb = FakeTMDB(show, {1: [{"episode_number": 7, "air_date": "2026-10-28"}]})
    # Watched up to 6 (e.g. marked as watched ahead): 7 is not the announced one.
    assert next_episode(tmdb, 1, 1, 6, TODAY) == {
        "season": 1,
        "episode": 7,
        "aired": False,
        "air_date": "2026-10-28",
    }
    assert tmdb.season_calls == [1]


def test_episode_counts_lagging_behind_use_the_announced_episode():
    show = {
        "seasons": [season(1, 8)],
        "last_episode_to_air": ep(1, 8, "2026-10-01"),
        "next_episode_to_air": ep(
            2, 1, "2026-10-19"
        ),  # out yesterday, counts not updated yet
    }
    assert next_episode(FakeTMDB(show), 1, 1, 8, TODAY) == {
        "season": 2,
        "episode": 1,
        "aired": True,
        "air_date": "2026-10-19",
    }


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(shared, "PATH", str(tmp_path / "shared.json"))
    return create_app(testing=True).test_client()


def _tmdb(client):
    with client.application.app_context():
        return services().tmdb


def test_next_episode_endpoint(client, monkeypatch):
    tmdb = _tmdb(client)
    shows = {1: SHOW, 2: {"seasons": [season(1, 3)], "last_episode_to_air": ep(1, 3)}}
    monkeypatch.setattr(tmdb, "show_summary", lambda tmdb_id: shows[tmdb_id])
    data = client.get("/api/next-episode?ids=1:1:10,tv:2:1:3,x:1,3:a:1").get_json()
    assert data["success"]
    assert data["next"] == {
        "1:1:10": {"season": 2, "episode": 1, "aired": True, "air_date": None}
    }


def test_because_you_watched_returns_poster_cards(client, monkeypatch):
    tmdb = _tmdb(client)
    asked = []

    def recommendations(media_type, tmdb_id, language=None):
        asked.append((media_type, tmdb_id, language))
        if tmdb_id == 9:
            return []
        return [
            {
                "id": 10 + tmdb_id,
                "title": "Arrival",
                "poster_path": "/a.jpg",
                "release_date": "2016-11-10",
                "vote_average": 7.9,
            },
            {"id": 99, "title": "No poster"},
        ]

    monkeypatch.setattr(tmdb, "recommendations", recommendations)
    data = client.get(
        "/api/because?keys=movie:1,tv:2,movie:1,movie:9,person:3,movie:x"
    ).get_json()
    assert [row["key"] for row in data["rows"]] == ["movie:1", "tv:2"]
    item = data["rows"][0]["items"][0]
    assert item == {
        **item,
        "id": 11,
        "type": "movie",
        "title": "Arrival",
        "year": 2016,
        "url": "/movie/11",
    }
    assert data["rows"][1]["items"][0]["url"] == "/show/12"
    assert len(data["rows"][0]["items"]) == 1  # posterless titles are left out
    assert {a[2] for a in asked} == {
        "en-US"
    }  # the visitor's language, also in worker threads


def test_collection_row_in_release_order_with_the_film_marked(client, monkeypatch):
    from streamfinder.views import collection_row

    tmdb = _tmdb(client)
    parts = [
        {
            "id": 3,
            "title": "Dune: Part Three",
            "poster_path": "/3.jpg",
            "release_date": "",
        },
        {
            "id": 2,
            "title": "Dune: Part Two",
            "poster_path": "/2.jpg",
            "release_date": "2024-02-27",
        },
        {
            "id": 1,
            "title": "Dune",
            "poster_path": "/1.jpg",
            "release_date": "2021-09-15",
        },
    ]
    monkeypatch.setattr(
        tmdb, "collection", lambda cid: {"name": "Dune Collection", "parts": parts}
    )
    with client.application.test_request_context():
        row = collection_row({"belongs_to_collection": {"id": 7, "name": "Dune"}}, 2)
        assert row["title"] == "Dune Collection"
        assert [(i["id"], i["current"]) for i in row["items"]] == [
            (1, False),
            (2, True),
            (3, False),
        ]
        assert collection_row({"belongs_to_collection": None}, 2) is None
        monkeypatch.setattr(tmdb, "collection", lambda cid: {"parts": parts[:1]})
        assert (
            collection_row({"belongs_to_collection": {"id": 7}}, 3) is None
        )  # one film is no series


def test_movie_page_shows_its_collection(client, monkeypatch):
    tmdb = _tmdb(client)
    details = {
        "id": 2,
        "title": "Dune: Part Two",
        "release_date": "2024-02-27",
        "poster_path": "/2.jpg",
        "runtime": 166,
        "belongs_to_collection": {"id": 7, "name": "Dune Collection"},
    }
    parts = [
        {
            "id": 1,
            "title": "Dune",
            "poster_path": "/1.jpg",
            "release_date": "2021-09-15",
        },
        {
            "id": 2,
            "title": "Dune: Part Two",
            "poster_path": "/2.jpg",
            "release_date": "2024-02-27",
        },
    ]
    monkeypatch.setattr(tmdb, "details", lambda media_type, tmdb_id: details)
    monkeypatch.setattr(
        tmdb, "collection", lambda cid: {"name": "Dune Collection", "parts": parts}
    )
    html = client.get("/movie/2").get_data(as_text=True)
    assert 'data-row="collection"' in html and "Dune Collection" in html
    assert 'class="poster is-current" href="/movie/2"' in html
    assert 'id="watched-btn"' in html


# -- mark as watched: what the browsers send, merged like any other progress change


def test_episode_marked_as_watched_reaches_other_devices():
    stored = {
        "progress": {
            "tv:5": {
                "season": 1,
                "episode": 3,
                "position": 600,
                "duration": 1500,
                "updated": 100,
                "episodes": {"1x3": {"position": 600, "duration": 1500}},
            }
        }
    }
    marked = {
        "progress": {
            "tv:5": {
                "season": 1,
                "episode": 4,
                "position": 1500,
                "duration": 1500,
                "updated": 200,
                "episodes": {
                    "1x3": {"position": 600, "duration": 1500},
                    "1x4": {"position": 1500, "duration": 1500, "watched": True},
                },
            }
        }
    }
    result = merge(stored, marked, 1_000)
    entry = result["progress"]["tv:5"]
    assert entry["episodes"]["1x4"]["watched"] and (
        entry["season"],
        entry["episode"],
    ) == (1, 4)
    # Another device still sending its older copy doesn't undo it...
    assert merge(result, stored, 1_000)["progress"]["tv:5"]["episodes"]["1x4"][
        "watched"
    ]
    # ...but unmarking later does.
    unmarked = {"progress": {"tv:5": {**stored["progress"]["tv:5"], "updated": 300}}}
    assert "1x4" not in merge(result, unmarked, 1_000)["progress"]["tv:5"]["episodes"]


def test_film_marked_as_watched_and_unmarked(client):
    marked = {
        "progress": {
            "movie:1": {
                "title": "Dune",
                "position": 9960,
                "duration": 9960,
                "watched": True,
                "updated": 100,
            }
        },
        "list": [],
        "listRemoved": {},
    }
    assert client.post("/api/shared", json=marked).get_json()["progress"]["movie:1"][
        "watched"
    ]
    unmarked = {
        "progress": {
            "movie:1": {
                "title": "Dune",
                "position": 0,
                "duration": 9960,
                "watched": False,
                "updated": 200,
            }
        },
        "list": [],
        "listRemoved": {},
    }
    client.post("/api/shared", json=unmarked)
    entry = client.get("/api/shared").get_json()["progress"]["movie:1"]
    assert entry["watched"] is False and entry["position"] == 0
