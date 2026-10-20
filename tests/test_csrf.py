"""Requests that change something only from the app's own pages (Origin / Referer)."""

import os

import pytest

from streamfinder import auth, config, create_app, shared


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(shared, "PATH", str(tmp_path / "shared.json"))
    monkeypatch.setattr(config, "ADMIN_USERNAME", "")
    monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "")
    auth._failures.clear()
    return create_app(testing=True).test_client()


LOGIN = {"username": "admin", "password": "admin"}
HOST = "192.168.1.5:5000"


def post(client, path="/api/admin/login", **headers):
    return client.post(path, json=LOGIN, headers={"Host": HOST, **headers})


@pytest.mark.parametrize(
    "headers",
    [
        {},  # curl, scripts, the Windows tray
        {"Origin": f"http://{HOST}"},  # fetch and sendBeacon from the app's pages
        {"Origin": "http://192.168.1.5:5000", "Referer": "http://evil.example/"},
        {"Referer": f"http://{HOST}/settings"},  # an older browser without Origin
        {"Origin": "HTTP://192.168.1.5:5000"},
        # Host and port count (like for cookies): https on the same port is this server.
        {"Origin": "https://192.168.1.5:5000"},
        # Behind a reverse proxy: the app sees its own address, the proxy passes the browser's.
        {
            "Host": "127.0.0.1:5000",
            "X-Forwarded-Host": "tv.home",
            "Origin": "https://tv.home",
        },
    ],
)
def test_own_pages_and_tools_pass(client, headers):
    assert post(client, **headers).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://evil.example"},
        {"Origin": "http://192.168.1.5:8090"},  # TorrServer's port is another origin
        {"Origin": "http://192.168.1.5"},  # port 80 is not 5000
        {"Origin": "null"},  # a sandboxed frame or a local file
        {"Referer": "http://evil.example/page"},
        {"Origin": "file://"},
    ],
)
def test_other_sites_are_refused(client, headers):
    response = post(client, **headers)
    assert response.status_code == 403
    assert response.get_json() == {
        "success": False,
        "error": "Refused: the request came from another website",
    }


def test_default_ports_match_a_host_without_one(client):
    response = client.post(
        "/api/admin/login",
        json=LOGIN,
        headers={"Host": "tv.home", "Origin": "http://tv.home:80"},
    )
    assert response.status_code == 200
    response = client.post(
        "/api/admin/login",
        json=LOGIN,
        headers={"Host": "tv.home", "Origin": "https://tv.home"},
    )
    assert response.status_code == 200


def test_reading_is_never_refused(client):
    response = client.get("/api/healthz", headers={"Origin": "http://evil.example"})
    assert response.status_code == 200


def test_every_state_changing_method_is_checked(client):
    bad = {"Host": HOST, "Origin": "http://evil.example"}
    for method in ("post", "put", "patch", "delete"):
        response = getattr(client, method)("/api/shared", json={}, headers=bad)
        assert response.status_code == 403, method
    assert not os.path.exists(shared.PATH)  # nothing was merged


def test_a_refused_request_changes_nothing(client):
    tv = {"progress": {"movie:1": {"position": 600, "updated": 100}}}
    response = client.post(
        "/api/shared",
        json=tv,
        headers={"Host": HOST, "Origin": "http://evil.example"},
    )
    assert response.status_code == 403
    assert client.get("/api/shared").get_json()["progress"] == {}
