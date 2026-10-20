import types

import pytest
import requests

from streamfinder import config, create_app, health


@pytest.fixture
def client(monkeypatch):
    app = create_app(testing=True)
    health._cache.clear()
    return app.test_client()


def test_health_groups_checks_and_lists_every_index(client, monkeypatch):
    calls = []

    def fine(svc):
        calls.append(1)
        return health._result("ok", "fine")

    def broken(svc):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        health,
        "CHECKS",
        (
            ("app", "system", "StreamFinder", fine),
            ("torrserver", "streaming", "TorrServer", fine),
            ("tmdb", "catalogue", "TMDB", broken),
        ),
    )
    with client.application.app_context():
        from streamfinder.services import services

        monkeypatch.setattr(
            services().torrents,
            "provider_status",
            lambda: [
                {"name": "Idx", "available": False, "url": "https://idx.example/api"}
            ],
        )
    data = client.get("/api/health").get_json()
    ids = [c["id"] for c in data["checks"]]
    assert ids[0] == "torrserver" and ids[-1] == "app"  # playback first, system last
    tmdb = next(c for c in data["checks"] if c["id"] == "tmdb")
    assert (
        tmdb["state"] == "fail" and "boom" in tmdb["detail"]
    )  # a broken check, not a 500
    index = next(c for c in data["checks"] if c["id"] == "index_idx")
    assert index["state"] == "fail" and index["group"] == "catalogue"
    assert data["summary"] == {"ok": 2, "warn": 0, "fail": 2, "off": 0}
    assert set(data["groups"]) == set(health.GROUPS)
    # Kept for a few seconds; ?fresh=1 checks again.
    client.get("/api/health")
    assert len(calls) == 2
    client.get("/api/health?fresh=1")
    assert len(calls) == 4


def test_texts_follow_the_visitors_language(client, monkeypatch):
    monkeypatch.setattr(
        health, "CHECKS", (("disk", "system", "health_name_disk", health.check_disk),)
    )
    with client.application.app_context():
        from streamfinder.services import services

        monkeypatch.setattr(services().torrents, "provider_status", list)
    client.set_cookie("lang", "cs")
    data = client.get("/api/health").get_json()
    assert (
        "volno" in data["checks"][0]["detail"] and data["groups"]["system"] == "Systém"
    )


def test_optional_services_without_keys_are_off(monkeypatch):
    for key in (
        "OPENSUBTITLES_API_KEY",
        "TRANSCRIBE_API_KEY",
        "TRANSCRIBE_API_URL",
        "NOTIFY_NTFY_URL",
        "NOTIFY_TELEGRAM_TOKEN",
    ):
        monkeypatch.setattr(config, key, "", raising=False)
    svc = types.SimpleNamespace(transcriber=types.SimpleNamespace(local_url=""))
    app = create_app(testing=True)
    with app.test_request_context():
        for check in (
            health.check_opensubtitles,
            health.check_speech_api,
            health.check_whisper,
            health.check_ntfy,
            health.check_telegram,
        ):
            assert check(svc)["state"] == "off", check.__name__


def test_missing_tmdb_key_fails_and_keys_never_show(monkeypatch):
    app = create_app(testing=True)
    monkeypatch.setattr(config, "TMDB_API_KEY", "")
    with app.test_request_context():
        assert health.check_tmdb(None)["state"] == "fail"
    monkeypatch.setattr(config, "NOTIFY_TELEGRAM_TOKEN", "123:secret-token")
    monkeypatch.setattr(config, "NOTIFY_TELEGRAM_CHAT", "42")

    def offline(*a, **k):
        raise requests.ConnectionError("no network")

    monkeypatch.setattr(health.requests, "get", offline)
    with app.test_request_context():
        result = health.check_telegram(None)
    assert result["state"] == "fail" and "secret-token" not in result["detail"]


def test_torrserver_check(monkeypatch):
    class Session:
        def get(self, url, timeout):
            return types.SimpleNamespace(status_code=200, text="1.2.MatriX.147")

    svc = types.SimpleNamespace(
        torrserver=types.SimpleNamespace(
            session=Session(), base_url="http://127.0.0.1:8090"
        )
    )
    with create_app(testing=True).test_request_context():
        result = health.check_torrserver(svc)
    assert result == {"state": "ok", "detail": "1.2.MatriX.147 · 127.0.0.1:8090"}
