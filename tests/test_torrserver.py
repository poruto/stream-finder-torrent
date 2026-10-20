"""TorrServer client and file selection (mocked HTTP), SkTorrent name rewriting."""

import pytest
import requests

from streamfinder import torrserver as torrserver_module
from streamfinder.matching import parse_release
from streamfinder.torrents.search import czech_kind
from streamfinder.torrents.sktorrent import SkTorrentProvider, _decode, scene_name
from streamfinder.torrserver import (
    TorrentFile,
    TorrServerClient,
    TorrServerError,
    extract_info_hash,
    normalize_magnet,
    select_video_file,
    video_files,
)

HASH = "08ada5a7a6183aae1e09d831df6748d566095a10"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise requests.ConnectionError("network disabled in tests")

    monkeypatch.setattr(requests.Session, "send", refuse)


# -- magnets ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, magnet",
    [
        (HASH.upper(), f"magnet:?xt=urn:btih:{HASH}"),
        (f"  {HASH}  ", f"magnet:?xt=urn:btih:{HASH}"),
        (
            f"magnet:?xt=urn:btih:{HASH.upper()}&dn=x",
            f"magnet:?xt=urn:btih:{HASH.upper()}&dn=x",
        ),
        ("magnet:?dn=no-hash", None),
        ("http://example.com/file.torrent", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_magnet(text, magnet):
    assert normalize_magnet(text) == magnet


def test_base32_hashes_are_converted_to_hex():
    info_hash = extract_info_hash("BCWX2UQYYNVK4HQJ5YNRS5WSRKO2CFIQ")
    assert len(info_hash) == 40 and int(info_hash, 16) >= 0
    assert (
        normalize_magnet("BCWX2UQYYNVK4HQJ5YNRS5WSRKO2CFIQ")
        == f"magnet:?xt=urn:btih:{info_hash}"
    )


def test_hash_of_wrong_length_is_refused():
    assert extract_info_hash("a" * 39) is None and extract_info_hash("a" * 41) is None


# -- which file plays ------------------------------------------------------------------------


def test_trailers_samples_and_extras_are_not_offered():
    files = [
        TorrentFile(1, "Movie/Movie.mkv", 10**9),
        TorrentFile(2, "Movie/Trailer.mkv", 10**7),
        TorrentFile(3, "Movie/Sample/sample.mkv", 5 * 10**7),
        TorrentFile(4, "Movie/Movie.nfo", 100),
    ]
    assert [f.index for f in video_files(files)] == [1]
    assert select_video_file(files).index == 1


def test_small_episodes_of_a_pack_are_kept():
    files = [
        TorrentFile(1, "Show/Show.S01E01.mkv", 10**9),
        TorrentFile(2, "Show/Show.S01E02.mkv", 10**6),
    ]
    assert [f.index for f in video_files(files)] == [1, 2]


def test_files_are_sorted_naturally():
    files = [
        TorrentFile(i, f"Show/Show.S01E{n}.mkv", 10**8)
        for i, n in enumerate(["10", "2", "1"])
    ]
    assert [f.path for f in video_files(files)] == [
        "Show/Show.S01E1.mkv",
        "Show/Show.S01E2.mkv",
        "Show/Show.S01E10.mkv",
    ]


def test_episode_season_comes_from_the_folder():
    files = [
        TorrentFile(0, "Show/Season 1/Show.E01.mkv", 5 * 10**8),
        TorrentFile(1, "Show/Season 2/Show.E01.mkv", 5 * 10**8),
        TorrentFile(2, "Show/Season 2/Show.E02.mkv", 5 * 10**8),
    ]
    assert select_video_file(files, season=2, episode=1).index == 1
    assert select_video_file(files, season=1, episode=1).index == 0


def test_missing_episode_falls_back_to_the_biggest_file():
    files = [
        TorrentFile(0, "Show.S01E01.mkv", 10**8),
        TorrentFile(1, "Show.S01E02.mkv", 2 * 10**8),
    ]
    assert select_video_file(files, season=1, episode=9).index == 1
    assert select_video_file([TorrentFile(0, "readme.txt", 1)]) is None
    assert select_video_file([]) is None


def test_numbered_files_in_a_season_folder():
    files = [
        TorrentFile(i, f"Show/Season 1/Show - {e:02d}.mkv", 5 * 10**8)
        for i, e in enumerate((1, 2, 3))
    ]
    assert select_video_file(files, season=1, episode=2).index == 1
    assert select_video_file(files, season=1, episode=3).index == 2


def test_numbered_names_with_titles_and_resolutions():
    files = [
        TorrentFile(0, "Show S01/01. Pilot 720p.mkv", 5 * 10**8),
        TorrentFile(1, "Show S01/02. The Return 720p.mkv", 6 * 10**8),
    ]
    assert select_video_file(files, season=1, episode=1).index == 0
    assert select_video_file(files, season=1, episode=2).index == 1


def test_file_name_and_dict():
    file = TorrentFile(3, "Dir\\Sub/Movie.mkv", 5)
    assert file.name == "Movie.mkv" and file.is_video and not file.is_subtitle
    assert file.to_dict() == {
        "index": 3,
        "path": "Dir\\Sub/Movie.mkv",
        "name": "Movie.mkv",
        "size": 5,
    }


# -- the client ------------------------------------------------------------------------------


def test_stream_url_escapes_the_name_and_can_use_a_public_host():
    client = TorrServerClient("http://ts:8090/", "stream/")
    file = TorrentFile(3, "Dir/Film (2020) [1080p].mkv", 1)
    assert client.stream_url("abc", file) == (
        "http://ts:8090/stream/Film%20%282020%29%20%5B1080p%5D.mkv?link=abc&index=3&play"
    )
    assert client.stream_url("abc", file, "http://192.168.1.2:8090/").startswith(
        "http://192.168.1.2:8090/stream/"
    )


class Answer:
    def __init__(self, data=None, status=200, chunks=()):
        self.data, self.status_code, self.chunks = data, status, list(chunks)
        self.content = b"x" if data is not None else b""
        self.text = str(data)
        self.closed = False

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, size):
        yield from self.chunks

    def close(self):
        self.closed = True


class FakeHTTP:
    def __init__(self, answers):
        self.answers, self.posts, self.gets = answers, [], []

    def post(self, url, json=None, timeout=None):
        self.posts.append(json)
        answer = self.answers(json)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return self.answers(kwargs)


def client_with(answers):
    client = TorrServerClient("http://ts:8090")
    client.session = FakeHTTP(answers)
    return client


@pytest.mark.parametrize(
    "error, code",
    [
        (requests.ConnectionError("refused"), "offline"),
        (requests.Timeout("slow"), "timeout"),
    ],
)
def test_connection_problems_have_codes(error, code):
    with pytest.raises(TorrServerError) as raised:
        client_with(lambda payload: error).add(f"magnet:?xt=urn:btih:{HASH}")
    assert raised.value.code == code


def test_add_falls_back_to_the_magnets_hash():
    client = client_with(lambda payload: Answer({}))
    assert client.add(f"magnet:?xt=urn:btih:{HASH.upper()}") == HASH
    assert client.session.posts[0]["save_to_db"] is True


def test_unloaded_torrent_is_added_again(monkeypatch):
    added = []

    def answers(payload):
        if payload["action"] == "add":
            added.append(payload["link"])
            return Answer({"hash": HASH})
        if not added:
            return Answer("not found", status=404)
        return Answer(
            {"file_stats": [{"id": 1, "path": "Movie/Movie.mkv", "length": 123}]}
        )

    files = client_with(answers).files(HASH)
    assert added == [f"magnet:?xt=urn:btih:{HASH}"]
    assert files == [TorrentFile(1, "Movie/Movie.mkv", 123)]


def test_files_waits_for_metadata(monkeypatch):
    replies = [
        Answer({}),
        Answer({}),
        Answer({"file_stats": [{"id": 1, "path": "a.mkv", "length": 1}]}),
    ]
    monkeypatch.setattr(torrserver_module.time, "sleep", lambda s: None)
    client = client_with(lambda payload: replies.pop(0))
    assert [f.index for f in client.files(HASH, wait=10)] == [1]


def test_files_gives_up_after_the_wait():
    client = client_with(lambda payload: Answer({}))
    assert client.files(HASH, wait=0) == []


def test_file_list_is_reused_for_known_indexes():
    calls = []

    def answers(payload):
        calls.append(payload)
        return Answer({"file_stats": [{"id": 1, "path": "a.mkv", "length": 1}]})

    client = client_with(answers)
    assert client.file(HASH, 1).path == "a.mkv"
    assert client.file(HASH, 1).path == "a.mkv"
    assert len(calls) == 1
    assert client.file(HASH, 7) is None  # unknown index: asked again
    assert len(calls) == 2


def test_server_error_is_reported():
    with pytest.raises(TorrServerError) as raised:
        client_with(lambda payload: Answer("boom", status=500)).drop(HASH)
    assert raised.value.code == "error" and "500" in str(raised.value)


def test_read_range_asks_for_exact_bytes_and_stops_early():
    answer = Answer(chunks=[b"abc", b"defgh", b"never read"])
    client = client_with(lambda kwargs: answer)
    data = client.read_range(HASH, TorrentFile(0, "a.mkv", 100), 10, 6)
    assert data == b"abcdef" and answer.closed
    assert client.session.gets[0][1]["headers"] == {"Range": "bytes=10-15"}


def test_read_range_never_past_the_end_of_the_file():
    client = client_with(lambda kwargs: Answer(chunks=[b"xyz"]))
    client.read_range(HASH, TorrentFile(0, "a.mkv", 12), 10, 100)
    assert client.session.gets[0][1]["headers"] == {"Range": "bytes=10-11"}


def test_big_files_are_not_read_as_subtitles():
    client = client_with(lambda kwargs: Answer(chunks=[b"x"]))
    with pytest.raises(TorrServerError):
        client.read_file(HASH, TorrentFile(0, "huge.srt", 50 * 1024 * 1024))


def test_is_online():
    assert client_with(lambda kwargs: Answer({}, 200)).is_online()
    assert not client_with(lambda kwargs: Answer({}, 500)).is_online()
    assert not TorrServerClient("http://ts:8090").is_online()  # no network at all


# -- SkTorrent names ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, flags, category, seasons, year",
    [
        (
            "Perníkový táta / Breaking Bad 1-5. série (2008)(CZ/EN)[1080p]",
            ["cz", "gb"],
            16,
            [1, 2, 3, 4, 5],
            2008,
        ),
        ("Přátelé 2. řada (1995)(CZ)", ["cz"], 16, [2], 1995),
        ("Série 4 Show (2010)", ["cz"], 16, [4], 2010),
        ("Kmotr / The Godfather (1972)(CZ)[1080p] = CSFD 92%", ["cz"], 1, [], 1972),
    ],
)
def test_scene_name_seasons_and_years(title, flags, category, seasons, year):
    info = parse_release(
        scene_name(
            title, flags, category, ["Breaking Bad", "The Godfather", "Friends", "Show"]
        )
    )
    assert info.seasons == seasons and info.year == year


def test_scene_name_keeps_the_title_we_look_for():
    name = scene_name(
        "Kmotr / The Godfather (1972)(CZ)[1080p] = CSFD 92%",
        ["cz"],
        1,
        ["The Godfather"],
    )
    assert parse_release(name).title == "the godfather" and "CSFD" not in name
    czech = scene_name("Kmotr / The Godfather (1972)(CZ)", ["cz"], 1, ["Kmotr"])
    assert parse_release(czech).title == "kmotr"


def test_scene_name_subtitled_category_and_html_entities():
    subs = scene_name("Shogun (2024)[1080p]", ["cz"], 15, ["Shogun"])
    assert czech_kind(parse_release(subs)) == "subs"
    assert "Film & Co" in scene_name("Film &amp; Co (2001)(SK)", ["sk"], 1, [])


def test_scene_name_slovak_flag():
    assert "SK" in scene_name("Film (2001)", ["sk"], 1, []).split()


def test_page_decoding_handles_both_encodings():
    assert _decode("Přátelé".encode("cp1250")) == "Přátelé"
    assert _decode("Přátelé".encode()) == "Přátelé"


def test_sktorrent_without_login_searches_nothing():
    from streamfinder.torrents.search import SearchQuery

    provider = SkTorrentProvider("", "", 5)
    assert not provider.enabled and not provider.is_available()
    assert provider.search(SearchQuery(title="Pelisky", titles=["Pelisky"])) == []
    assert provider.extra_trackers(HASH) == []


def test_sktorrent_unreachable_is_a_connection_error():
    from streamfinder.torrents.search import SearchQuery

    provider = SkTorrentProvider("user", "pass", 5)
    with pytest.raises(ConnectionError):
        provider.search(SearchQuery(title="Pelisky", titles=["Pelisky"]))
    assert not provider.is_available()


def test_personal_announce_url_is_read_from_the_torrent(monkeypatch):
    provider = SkTorrentProvider("user", "pass", 5)
    provider._seen[HASH] = None
    url = b"https://sktorrent.eu/torrent/announce.php?pid=secret"
    torrent = (
        b"d8:announce" + str(len(url)).encode() + b":" + url + b"4:infod4:name1:xee"
    )

    class Download:
        content = torrent

        def raise_for_status(self):
            pass

    monkeypatch.setattr(provider, "_login", lambda force=False: None)
    monkeypatch.setattr(provider.session, "get", lambda *a, **k: Download())
    trackers = provider.extra_trackers(HASH.upper())
    assert trackers[0] == url.decode() and trackers[-1].startswith("udp://")


def test_announce_lookup_failure_still_adds_public_trackers(monkeypatch):
    provider = SkTorrentProvider("user", "pass", 5)
    provider._seen[HASH] = None
    trackers = provider.extra_trackers(HASH)  # no network: lookup fails
    assert trackers and all(t.startswith("udp://") for t in trackers)
