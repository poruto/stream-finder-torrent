import threading

from streamfinder import create_app
from streamfinder.services import services
from streamfinder.torrserver import TorrentFile

MAGNET = "magnet:?xt=urn:btih:" + "c" * 40


class FakeTorrServer:
    def __init__(self):
        self.preloaded = threading.Event()
        self.file = None

    def add(self, magnet, title="", poster=""):
        self.added = (magnet, title)
        return "c" * 40

    def files(self, info_hash, wait=0):
        return [
            TorrentFile(index=1, path="Show.S01E01.mkv", size=900_000_000),
            TorrentFile(index=2, path="Show.S01E02.mkv", size=900_000_000),
        ]

    def preload(self, info_hash, file, timeout=180):
        self.file = file
        self.preloaded.set()


def test_preload_answers_at_once_and_warms_up_the_episode(monkeypatch):
    app = create_app(testing=True)
    fake = FakeTorrServer()
    with app.app_context():
        monkeypatch.setattr(services(), "torrserver", fake)
    response = app.test_client().post(
        "/api/preload",
        json={"magnet": MAGNET, "season": 1, "episode": 2, "title": "Show S01E02"},
    )
    assert response.get_json() == {"success": True, "hash": "c" * 40}
    assert fake.added[1] == "Show S01E02"
    assert fake.preloaded.wait(5)
    assert fake.file.index == 2  # the next episode's file, not the first one


def test_preload_rejects_a_bad_magnet():
    response = (
        create_app(testing=True)
        .test_client()
        .post("/api/preload", json={"magnet": "nope"})
    )
    assert response.status_code == 400
