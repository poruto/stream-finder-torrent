import pytest

from streamfinder import auth, config, create_app
from streamfinder.library import folder_size
from streamfinder.services import services


class FakeTorrServer:
    def __init__(self):
        self.torrents = [
            {"hash": "a" * 40, "title": "Dune", "torrent_size": 5_000_000_000, "timestamp": 200, "stat": 3},
            {"hash": "b" * 40, "title": "Old", "torrent_size": 1_000, "timestamp": 100, "stat": 5},
        ]
        self.removed = []

    def _post(self, payload, timeout=None):
        if payload["action"] == "list":
            return list(self.torrents)
        if payload["action"] == "rem":
            self.removed.append(payload["hash"])
            self.torrents = [t for t in self.torrents if t["hash"] != payload["hash"]]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "settings.env"))
    monkeypatch.setattr(config, "ADMIN_USERNAME", "")
    monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "")
    monkeypatch.setattr(config, "SUBTITLE_CACHE_DIR", str(tmp_path / "subtitles"))
    auth._failures.clear()
    app = create_app(testing=True)
    fake = FakeTorrServer()
    with app.app_context():
        monkeypatch.setattr(services(), "torrserver", fake)
    client = app.test_client()
    client.fake = fake
    return client


def login(client):
    assert client.post("/api/admin/login", json={"username": "admin", "password": "admin"}).status_code == 200


def test_storage_needs_admin(client):
    assert client.get("/api/library").status_code == 401
    assert client.post("/api/library/remove", json={"all": True}).status_code == 401
    assert not client.fake.removed


def test_lists_newest_first_with_sizes(client):
    login(client)
    data = client.get("/api/library").get_json()
    assert [t["title"] for t in data["torrents"]] == ["Dune", "Old"]
    assert data["torrents"][0]["active"] and not data["torrents"][1]["active"]
    assert data["free"] > 0


def test_remove_one_and_all(client):
    login(client)
    client.post("/api/library/remove", json={"hash": "b" * 40})
    assert client.fake.removed == ["b" * 40]
    assert client.post("/api/library/remove", json={"all": True}).get_json()["removed"] == 1
    assert client.get("/api/library").get_json()["torrents"] == []


def test_clear_subtitles_frees_the_cache(client, tmp_path):
    folder = tmp_path / "subtitles"
    (folder / "x").mkdir(parents=True)
    (folder / "a.vtt").write_text("WEBVTT\n" * 100)
    (folder / "x" / "b.vtt").write_text("WEBVTT\n" * 10)
    before = folder_size(str(folder))
    login(client)
    freed = client.post("/api/library/clear-subtitles", json={}).get_json()["freed"]
    assert before > 0 and freed == before and folder_size(str(folder)) == 0
