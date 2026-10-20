import json

import pytest

from streamfinder import auth, config, create_app, notify, shared
from streamfinder.library import folder_size
from streamfinder.services import services


class FakeTorrServer:
    def __init__(self):
        self.torrents = [
            {
                "hash": "a" * 40,
                "title": "Dune",
                "torrent_size": 5_000_000_000,
                "timestamp": 200,
                "stat": 3,
            },
            {
                "hash": "b" * 40,
                "title": "Old",
                "torrent_size": 1_000,
                "timestamp": 100,
                "stat": 5,
            },
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
    monkeypatch.setattr(shared, "PATH", str(tmp_path / "shared.json"))
    monkeypatch.setattr(notify, "STATE", str(tmp_path / "notified.json"))
    from streamfinder import library

    monkeypatch.setattr(
        library, "ENV_FILE", str(tmp_path / ".env")
    )  # never the real one
    auth._failures.clear()
    app = create_app(testing=True)
    fake = FakeTorrServer()
    with app.app_context():
        monkeypatch.setattr(services(), "torrserver", fake)
    client = app.test_client()
    client.fake = fake
    return client


def login(client):
    assert (
        client.post(
            "/api/admin/login", json={"username": "admin", "password": "admin"}
        ).status_code
        == 200
    )


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
    assert (
        client.post("/api/library/remove", json={"all": True}).get_json()["removed"]
        == 1
    )
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


# -- backup and restore -----------------------------------------------------------------

HOUSEHOLD = {
    "progress": {"movie:1": {"title": "Dune", "position": 600, "updated": 100}},
    "list": {"tv:5": {"key": "tv:5", "added": 90}},
    "listRemoved": {},
    "defaultProfile": {"name": "Dad", "color": 2},
    "profiles": {"0a1b2c3d": {"name": "Eva", "color": 4, "created": 10, "kids": True}},
    "data": {
        "0a1b2c3d": {
            "progress": {"tv:7": {"season": 1, "episode": 3, "updated": 50}},
            "list": {},
            "listRemoved": {},
        }
    },
}


@pytest.fixture
def secrets_kept(monkeypatch):
    """Settings a restore writes are put back after the test (config and os.environ)."""
    for key in config.EDITABLE_KEYS + config.SECRET_SETTINGS:
        monkeypatch.setenv(key, "")
        monkeypatch.setattr(config, key, getattr(config, key, ""), raising=False)


def _household(tmp_path):
    shared.write_json(shared.PATH, HOUSEHOLD)
    notify_state = {"5": "S01E02"}
    shared.write_json(notify.STATE, notify_state)
    (tmp_path / "settings.env").write_text(
        "TRANSCRIBE_PROVIDER=local\nOPENSUBTITLES_API_KEY=os-secret\nSECRET_KEY=abc\n",
        encoding="utf-8",
    )


def test_backup_needs_admin(client):
    assert client.get("/api/backup").status_code == 401
    assert client.post("/api/backup/restore", json={}).status_code == 401


def test_backup_is_one_file_without_keys(client, tmp_path):
    _household(tmp_path)
    login(client)
    response = client.get("/api/backup")
    assert response.status_code == 200
    assert response.headers["Content-Disposition"].startswith(
        'attachment; filename="streamfinder-backup-'
    )
    data = json.loads(response.data)
    assert data["streamfinder_backup"] == 1 and data["keys_included"] is False
    assert data["shared"] == HOUSEHOLD  # every profile, its name and colour
    assert data["notified"] == {"5": "S01E02"}
    # Only the Settings page's own settings; no keys, never the Flask secret.
    assert data["settings"] == {"TRANSCRIBE_PROVIDER": "local"}
    assert "os-secret" not in response.get_data(as_text=True)


def test_backup_with_keys_when_asked(client, tmp_path):
    _household(tmp_path)
    login(client)
    data = client.get("/api/backup?keys=1").get_json()
    assert data["keys_included"] is True
    assert data["settings"] == {
        "TRANSCRIBE_PROVIDER": "local",
        "OPENSUBTITLES_API_KEY": "os-secret",
    }


def test_restore_brings_everything_back(client, tmp_path, secrets_kept):
    _household(tmp_path)
    login(client)
    backup = client.get("/api/backup?keys=1").get_json()
    # A new SD card: nothing but one title watched here since.
    for path in (shared.PATH, notify.STATE, str(tmp_path / "settings.env")):
        (tmp_path / path).unlink()
    newer = {"movie:1": {"title": "Dune", "position": 900, "updated": 200}}
    client.post("/api/shared", json={"progress": newer, "list": [], "listRemoved": {}})

    result = client.post("/api/backup/restore", json=backup).get_json()
    assert result == {
        "success": True,
        "profiles": 2,
        "titles": 3,
        "settings": ["OPENSUBTITLES_API_KEY", "TRANSCRIBE_PROVIDER"],
    }
    home = client.get("/api/shared").get_json()
    assert home["progress"]["movie:1"]["position"] == 900  # the newer entry wins
    assert [i["key"] for i in home["list"]] == ["tv:5"]
    profiles = client.get("/api/profiles").get_json()["profiles"]
    assert [(p["id"], p["name"], p["color"]) for p in profiles] == [
        ("", "Dad", 2),
        ("0a1b2c3d", "Eva", 4),
    ]
    assert profiles[1]["kids"] is True
    eva = client.get("/api/shared?profile=0a1b2c3d").get_json()
    assert eva["progress"]["tv:7"]["episode"] == 3
    assert notify._read_notified() == {"5": "S01E02"}
    assert config.OPENSUBTITLES_API_KEY == "os-secret"
    assert config.TRANSCRIBE_PROVIDER == "local"
    assert "TRANSCRIBE_PROVIDER=local" in (tmp_path / "settings.env").read_text()


def test_restore_twice_changes_nothing_more(client, tmp_path, secrets_kept):
    _household(tmp_path)
    login(client)
    backup = client.get("/api/backup").get_json()
    client.post("/api/backup/restore", json=backup)
    before = shared._read()
    client.post("/api/backup/restore", json=backup)
    assert shared._read() == before


@pytest.mark.parametrize(
    "body",
    [
        {"shared": {}},  # not made by /api/backup
        {"streamfinder_backup": 99, "shared": {}},  # a newer format
        {"streamfinder_backup": 1, "shared": []},
        {"streamfinder_backup": 1, "shared": {}, "settings": ["TMDB_API_KEY"]},
    ],
)
def test_restore_refuses_other_files(client, body):
    login(client)
    response = client.post("/api/backup/restore", json=body)
    assert response.status_code == 400
    assert response.get_json() == {
        "success": False,
        "error": "This file is not a StreamFinder backup",
    }


def test_restore_keeps_only_sane_parts(client, tmp_path, secrets_kept):
    login(client)
    backup = {
        "streamfinder_backup": 1,
        "shared": {
            "progress": {"movie:1": "nope", "movie:2": {"updated": 5}},
            "list": ["nope"],
            "profiles": {
                "../etc": {"name": "Bad id"},
                "11111111": "not a profile",
                **{f"2222222{i}": {"name": f"P{i}"} for i in range(10)},
            },
            "data": [],
        },
        "notified": {"5": "S01E01", "x": "S01E01", "6": 7},
        "settings": {
            "SECRET_KEY": "stolen",  # not a setting of the Settings page
            "TRANSCRIBE_API_URL": "http://x\nTMDB_API_KEY=evil",
            "TRANSCRIBE_PROVIDER": "api",
        },
    }
    result = client.post("/api/backup/restore", json=backup).get_json()
    assert result["settings"] == ["TRANSCRIBE_PROVIDER"]
    assert (tmp_path / "settings.env").read_text() == "TRANSCRIBE_PROVIDER=api\n"
    profiles = client.get("/api/profiles").get_json()["profiles"]
    assert len(profiles) == shared.MAX_PROFILES  # the default one and seven more
    assert client.get("/api/shared").get_json()["progress"] == {
        "movie:2": {"updated": 5}
    }
    assert notify._read_notified() == {"5": "S01E01"}


def test_backup_also_has_the_keys_the_installer_put_into_env(client, tmp_path):
    lines = ["TMDB_API_KEY=from-installer", "TRANSCRIBE_ENABLED=true", ""]
    (tmp_path / ".env").write_text(chr(10).join(lines), encoding="utf-8")
    (tmp_path / "settings.env").write_text("TRANSCRIBE_ENABLED=false", encoding="utf-8")
    login(client)
    plain = client.get("/api/backup").get_json()["settings"]
    assert plain["TRANSCRIBE_ENABLED"] == "false"  # the Settings page wins over .env
    assert "TMDB_API_KEY" not in plain
    with_keys = client.get("/api/backup?keys=1").get_json()["settings"]
    assert with_keys["TMDB_API_KEY"] == "from-installer"
