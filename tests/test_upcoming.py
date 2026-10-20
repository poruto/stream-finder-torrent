import datetime

import pytest

from streamfinder import create_app
from streamfinder.services import services
from streamfinder.upcoming import movie_events, show_events

TODAY = "2030-03-01"
UNTIL = "2030-04-30"


class FakeTMDB:
    def __init__(self, pages):
        self.pages = pages

    def _get(self, path, ttl=0, **params):
        return self.pages[path]


def test_show_lists_the_episodes_airing_soon():
    tmdb = FakeTMDB(
        {
            "/tv/7": {
                "name": "Show",
                "poster_path": "/p.jpg",
                "next_episode_to_air": {"season_number": 3},
            },
            "/tv/7/season/3": {
                "episodes": [
                    {"episode_number": 1, "air_date": "2030-02-20", "name": "Aired"},
                    {"episode_number": 2, "air_date": "2030-03-05", "name": "Two"},
                    {"episode_number": 3, "air_date": "2030-06-01", "name": "Far away"},
                    {"episode_number": 4, "air_date": None},
                ]
            },
        }
    )
    events = show_events(tmdb, 7, TODAY, UNTIL)
    assert [(e["season"], e["episode"], e["name"]) for e in events] == [(3, 2, "Two")]
    assert not events[0]["premiere"]


def test_ended_show_has_no_events():
    tmdb = FakeTMDB({"/tv/8": {"name": "Ended", "next_episode_to_air": None}})
    assert show_events(tmdb, 8, TODAY, UNTIL) == []


def test_movie_gets_cinema_and_digital_release():
    release_dates = {
        "results": [
            {
                "iso_3166_1": "US",
                "release_dates": [
                    {"type": 3, "release_date": "2030-03-10T00:00:00.000Z"},
                    {"type": 4, "release_date": "2030-04-20T00:00:00.000Z"},
                ],
            },
            {
                "iso_3166_1": "CZ",
                "release_dates": [
                    {"type": 4, "release_date": "2030-04-15T00:00:00.000Z"}
                ],
            },
        ]
    }
    tmdb = FakeTMDB(
        {
            "/movie/5": {
                "title": "Film",
                "release_date": "2030-03-10",
                "release_dates": release_dates,
            }
        }
    )
    events = movie_events(tmdb, 5, TODAY, UNTIL)
    assert [(e["kind"], e["date"]) for e in events] == [
        ("cinema", "2030-03-10"),
        ("digital", "2030-04-15"),
    ]


def test_movie_already_out_has_no_events():
    tmdb = FakeTMDB(
        {
            "/movie/6": {
                "title": "Old",
                "release_date": "2001-01-01",
                "release_dates": {"results": []},
            }
        }
    )
    assert movie_events(tmdb, 6, TODAY, UNTIL) == []


@pytest.fixture
def client():
    return create_app(testing=True).test_client()


def test_calendar_endpoint_sorts_by_date(client, monkeypatch):
    today = datetime.date.today()
    soon = (today + datetime.timedelta(days=3)).isoformat()
    sooner = (today + datetime.timedelta(days=1)).isoformat()
    pages = {
        "/tv/1": {"name": "B show", "next_episode_to_air": {"season_number": 1}},
        "/tv/1/season/1": {"episodes": [{"episode_number": 1, "air_date": soon}]},
        "/movie/2": {
            "title": "A film",
            "release_date": sooner,
            "release_dates": {"results": []},
        },
    }
    with client.application.app_context():
        monkeypatch.setattr(services(), "tmdb", FakeTMDB(pages))
    data = client.get("/api/calendar?shows=1&movies=2,x").get_json()
    assert [(e["title"], e["kind"]) for e in data["events"]] == [
        ("A film", "cinema"),
        ("B show", "episode"),
    ]
    assert data["events"][1]["premiere"] and data["events"][1]["url"] == "/show/1"


def test_calendar_page_renders(client):
    assert client.get("/calendar").status_code == 200
