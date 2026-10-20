import pytest

from streamfinder import create_app, shared
from streamfinder.shared import merge

DAY = 86400 * 1000
NOW = 1_800_000_000_000


def test_newer_progress_wins_per_title():
    stored = {"progress": {"movie:1": {"position": 100, "updated": 10}, "tv:2": {"position": 5, "updated": 50}}}
    incoming = {"progress": {"movie:1": {"position": 900, "updated": 20}, "tv:2": {"position": 1, "updated": 40}}}
    result = merge(stored, incoming, NOW)
    assert result["progress"]["movie:1"]["position"] == 900  # the phone watched further
    assert result["progress"]["tv:2"]["position"] == 5  # an older copy doesn't win


def test_removed_title_is_not_brought_back():
    stored = {"progress": {"movie:1": {"removed": True, "updated": NOW - 1000}}}
    incoming = {"progress": {"movie:1": {"position": 100, "updated": NOW - 2000}}}
    assert merge(stored, incoming, NOW)["progress"]["movie:1"]["removed"]


def test_my_list_add_and_remove_across_devices():
    stored = {"list": {"movie:1": {"key": "movie:1", "added": 10}}}
    # Another device removed it later, and added a different title.
    incoming = {"list": [{"key": "movie:2", "added": 30}], "listRemoved": {"movie:1": 20}}
    result = merge(stored, incoming, NOW)
    assert set(result["list"]) == {"movie:2"}
    # Added again after the removal: back in the list.
    result = merge(result, {"list": [{"key": "movie:1", "added": 40}]}, NOW)
    assert set(result["list"]) == {"movie:1", "movie:2"}


def test_old_markers_are_dropped():
    stored = {"progress": {"movie:1": {"removed": True, "updated": NOW - 100 * DAY}}, "listRemoved": {"tv:3": NOW - 100 * DAY}}
    result = merge(stored, {}, NOW)
    assert result["progress"] == {} and result["listRemoved"] == {}


def test_garbage_is_ignored():
    result = merge({}, {"progress": {"x": "nope"}, "list": ["nope", {"no": "key"}], "listRemoved": {"y": "z"}}, NOW)
    assert result == {"progress": {}, "list": {}, "listRemoved": {}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(shared, "PATH", str(tmp_path / "shared.json"))
    return create_app(testing=True).test_client()


def test_two_devices_share_progress_and_list(client):
    tv = {"progress": {"movie:1": {"title": "Dune", "position": 600, "updated": 100}}, "list": [{"key": "tv:5", "added": 90}], "listRemoved": {}}
    assert client.post("/api/shared", json=tv).get_json()["success"]
    phone = client.post("/api/shared", json={"progress": {}, "list": [], "listRemoved": {}}).get_json()
    assert phone["progress"]["movie:1"]["position"] == 600
    assert [i["key"] for i in phone["list"]] == ["tv:5"]
    assert client.get("/api/shared").get_json()["progress"]["movie:1"]["title"] == "Dune"


def test_new_episodes_lists_each_shows_last_episode(client, monkeypatch):
    from streamfinder.services import services

    with client.application.app_context():
        tmdb = services().tmdb
    latest = {1: {"season": 2, "episode": 5, "air_date": "2026-10-01"}, 2: None}
    monkeypatch.setattr(tmdb, "latest_episode", lambda tmdb_id: latest.get(tmdb_id))
    data = client.get("/api/new-episodes?ids=1,2,x").get_json()
    assert data["latest"] == {"1": latest[1]}


def test_profiles_keep_their_own_progress_and_list(client):
    added = client.post("/api/profiles", json={"action": "add", "name": "Kids", "color": 3}).get_json()
    kid = added["id"]
    assert [p["name"] for p in added["profiles"]][1:] == ["Kids"]
    mine = {"progress": {"movie:1": {"position": 600, "updated": 100}}, "list": [], "listRemoved": {}}
    theirs = {"progress": {"tv:2": {"position": 60, "updated": 100}}, "list": [{"key": "tv:2", "added": 90}], "listRemoved": {}}
    client.post("/api/shared", json=mine)
    client.post(f"/api/shared?profile={kid}", json=theirs)
    assert list(client.get("/api/shared").get_json()["progress"]) == ["movie:1"]
    kids = client.get(f"/api/shared?profile={kid}").get_json()
    assert list(kids["progress"]) == ["tv:2"] and kids["list"][0]["key"] == "tv:2"


def test_removed_profile_answers_404_and_loses_its_data(client):
    kid = client.post("/api/profiles", json={"action": "add", "name": "Guest"}).get_json()["id"]
    client.post(f"/api/shared?profile={kid}", json={"progress": {"tv:9": {"updated": 5}}})
    assert client.post("/api/profiles", json={"action": "remove", "id": kid}).status_code == 200
    assert client.get(f"/api/shared?profile={kid}").status_code == 404
    assert client.get("/api/shared?profile=nope").status_code == 404


def test_profile_needs_a_name_and_the_default_can_be_renamed(client):
    assert client.post("/api/profiles", json={"action": "add", "name": "  "}).status_code == 400
    assert client.post("/api/profiles", json={"action": "remove", "id": ""}).status_code == 404
    data = client.post("/api/profiles", json={"action": "edit", "id": "", "name": "Novákovi", "color": 4}).get_json()
    assert data["profiles"][0] == {"id": "", "name": "Novákovi", "color": 4}


def test_notifier_sees_every_profile(client):
    from streamfinder import notify

    kid = client.post("/api/profiles", json={"action": "add", "name": "Kids"}).get_json()["id"]
    client.post("/api/shared", json={"progress": {"tv:5": {"season": 1, "episode": 3, "updated": 9}}})
    client.post(f"/api/shared?profile={kid}", json={"progress": {"tv:5": {"season": 1, "episode": 1, "updated": 9}}})
    parts = shared.all_parts(shared._read())
    watched = min(notify.followed_shows(p).get(5, 10**9) for p in parts)
    assert watched == notify.episode_number(1, 1)  # still new for whoever is behind
