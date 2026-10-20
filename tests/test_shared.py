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
