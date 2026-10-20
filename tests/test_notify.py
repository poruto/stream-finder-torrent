import datetime

import pytest

from streamfinder import auth, config, create_app, notify
from streamfinder.services import services

TODAY = datetime.date(2030, 3, 10)


def run_check(app):
    with app.app_context():
        return notify.check_once(TODAY)


def test_followed_shows_take_the_furthest_episode_watched():
    state = {
        "progress": {
            "tv:1": {"season": 1, "episode": 3, "episodes": {"1x3": {}, "2x1": {}}},
            "tv:2": {"removed": True, "updated": 1},
            "movie:3": {"position": 10},
        },
        "list": {
            "tv:4": {"key": "tv:4", "type": "tv", "id": 4},
            "movie:5": {"type": "movie", "id": 5},
        },
    }
    assert notify.followed_shows(state) == {1: notify.episode_number(2, 1), 4: 0}


def test_due_only_for_a_recent_episode_after_the_watched_one():
    latest = {"season": 2, "episode": 2, "air_date": "2030-03-08"}
    assert notify.due(latest, notify.episode_number(2, 1), TODAY)
    assert not notify.due(latest, notify.episode_number(2, 2), TODAY)  # already watched
    assert not notify.due({**latest, "air_date": "2030-01-01"}, 0, TODAY)  # too old
    assert not notify.due(
        {**latest, "air_date": "2030-03-20"}, 0, TODAY
    )  # not aired yet
    assert not notify.due(None, 0, TODAY)


class FakeTMDB:
    def latest_episode(self, tmdb_id):
        return {"season": 1, "episode": 5, "air_date": "2030-03-09"}

    def details(self, media_type, tmdb_id):
        return {"name": "Šógun"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "STATE", str(tmp_path / "notified.json"))
    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "settings.env"))
    monkeypatch.setattr(config, "ADMIN_USERNAME", "")
    monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "")
    monkeypatch.setattr(config, "PUBLIC_URL", "http://tv.local:5000")
    auth._failures.clear()
    app = create_app(testing=True)
    with app.app_context():
        monkeypatch.setattr(services(), "tmdb", FakeTMDB())
    monkeypatch.setattr(
        notify.shared,
        "_read",
        lambda: {"progress": {"tv:9": {"season": 1, "episode": 4}}},
    )
    return app


def test_a_playable_episode_is_announced_once(app, monkeypatch):
    sent = []
    monkeypatch.setattr(
        notify, "send", lambda title, text, link="": sent.append((title, text, link))
    )
    monkeypatch.setattr(
        notify, "playable", lambda *a: {"quality": "1080p", "seeders": 42}
    )
    assert run_check(app) == ["Šógun S01E05"]
    title, text, link = sent[0]
    assert "Šógun" in title and "S01E05" in text and "42" in text
    assert link == "http://tv.local:5000/show/9?play=1&s=1&e=5"
    assert run_check(app) == []  # not again
    assert len(sent) == 1


def test_nothing_to_play_yet_is_asked_again_later(app, monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "send", lambda *a, **k: sent.append(a))
    monkeypatch.setattr(notify, "playable", lambda *a: None)
    assert run_check(app) == [] and not sent
    monkeypatch.setattr(
        notify, "playable", lambda *a: {"quality": "720p", "seeders": 3}
    )
    assert run_check(app) == ["Šógun S01E05"]


def test_ntfy_gets_the_title_and_link_as_parameters(monkeypatch):
    calls = []

    class Response:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(config, "NOTIFY_NTFY_URL", "https://ntfy.sh/secret")
    monkeypatch.setattr(config, "NOTIFY_TELEGRAM_TOKEN", "")
    monkeypatch.setattr(
        notify.requests, "post", lambda url, **kw: calls.append((url, kw)) or Response()
    )
    notify.send("Nový díl: Šógun", "S01E05", "http://x/show/9")
    url, kw = calls[0]
    assert url == "https://ntfy.sh/secret"
    assert kw["params"] == {
        "title": "Nový díl: Šógun",
        "tags": "tv",
        "click": "http://x/show/9",
    }
    assert kw["data"] == b"S01E05"


def test_settings_need_admin_and_save_the_channel(app, monkeypatch):
    client = app.test_client()
    assert client.get("/api/notify").status_code == 401
    assert (
        client.post(
            "/api/admin/login", json={"username": "admin", "password": "admin"}
        ).status_code
        == 200
    )
    assert (
        client.post("/api/notify", json={"ntfy_url": "ftp://nope"}).status_code == 400
    )
    data = client.post(
        "/api/notify", json={"ntfy_url": "https://ntfy.sh/abc"}
    ).get_json()
    assert data["ntfy"] == "https://ntfy.sh/abc"
    assert (
        "NOTIFY_NTFY_URL=https://ntfy.sh/abc"
        in open(config.SETTINGS_FILE, encoding="utf-8").read()
    )
    monkeypatch.setattr(notify, "send", lambda *a: None)
    assert client.post("/api/notify", json={"test": True}).status_code == 200
    assert (
        client.post("/api/notify", json={"remove": ["ntfy"]}).get_json()["ntfy"] == ""
    )


def test_settings_page_has_the_form_for_the_admin(app):
    client = app.test_client()
    client.post("/api/admin/login", json={"username": "admin", "password": "admin"})
    page = client.get("/settings").get_data(as_text=True)
    assert 'id="notify-form"' in page and "js/notify.js" in page
