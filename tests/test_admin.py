import pytest

from streamfinder import auth, config, create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "settings.env"))
    monkeypatch.setattr(config, "ADMIN_USERNAME", "")
    monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "")
    monkeypatch.setattr(config, "TMDB_API_KEY", "test-key")
    auth._failures.clear()
    return create_app(testing=True).test_client()


def login(client, username="admin", password="admin"):
    return client.post("/api/admin/login", json={"username": username, "password": password})


def test_keys_need_admin(client):
    assert client.get("/api/keys").status_code == 401
    assert client.post("/api/keys", json={"tmdb_api_key": "x"}).status_code == 401
    assert login(client).status_code == 200
    assert client.get("/api/keys").get_json()["tmdb"] is True


def test_change_password_replaces_default(client, tmp_path):
    login(client)
    response = client.post(
        "/api/admin/credentials", json={"current_password": "admin", "username": "me", "new_password": "longer-pass$1"}
    )
    assert response.status_code == 200
    stored = (tmp_path / "settings.env").read_text()
    assert "longer-pass" not in stored and "ADMIN_PASSWORD_HASH=" in stored
    client.post("/api/admin/logout", json={})
    assert login(client).status_code == 401
    assert login(client, "me", "longer-pass$1").status_code == 200


def test_rejects_weak_or_default_passwords(client):
    login(client)
    for password in ("short", "admin"):
        response = client.post("/api/admin/credentials", json={"current_password": "admin", "new_password": password})
        assert response.status_code == 400


def test_locks_out_after_repeated_failures(client, monkeypatch):
    monkeypatch.setattr(auth.time, "sleep", lambda _: None)
    for _ in range(auth.MAX_FAILURES):
        login(client, password="wrong")
    assert login(client).status_code == 401  # even the right password waits out the lockout


def test_first_run_redirects_to_settings(client, monkeypatch):
    monkeypatch.setattr(config, "TMDB_API_KEY", "")
    response = client.get("/movies")
    assert response.status_code == 302 and "/settings?setup=1" in response.headers["Location"]
    assert client.get("/settings").status_code == 200
