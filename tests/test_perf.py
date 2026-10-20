"""The speed-ups change nothing visible: same pages, same subtitles, same cache rules."""

import gzip
import json
import random
import re
import time

import pytest
import requests
from flask import Flask, Response, render_template_string

from streamfinder import compression, config, create_app, i18n
from streamfinder import tmdb as tmdb_module
from streamfinder.subtitles import formats
from streamfinder.subtitles.service import SubtitleService
from streamfinder.tmdb import TMDBClient, TMDBError

GZIP = {"Accept-Encoding": "gzip, deflate, br"}


class Answer:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return json.loads(json.dumps(self.data))


class FakeHTTP:
    """TMDB stand-in: every path answers {"path": ..., "language": ...}; records calls."""

    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def get(self, url, params=None, timeout=None):
        path = url.removeprefix(TMDBClient.BASE_URL)
        self.calls.append((path, dict(params or {})))
        if path in self.fail:
            return Answer({}, 500)
        if path.endswith("/external_ids"):
            tmdb_id = int(path.split("/")[2])
            return Answer({"imdb_id": f"tt{tmdb_id:07d}" if tmdb_id % 3 else None})
        return Answer({"path": path, "language": params["language"], "results": []})


def client(language="en-US", **kwargs):
    c = TMDBClient("secret-key", language)
    c.session = FakeHTTP(**kwargs)
    return c


# -- TMDB cache ----------------------------------------------------------------------


def test_full_cache_forgets_the_oldest_answers_not_everything(monkeypatch):
    monkeypatch.setattr(tmdb_module, "CACHE_SIZE", 3)
    c = client()
    for n in range(3):
        c._get(f"/movie/{n}")
    c._get("/movie/0")  # read again: now the most recent
    c._get("/movie/3")  # one too many: /movie/1 goes
    calls = len(c.session.calls)
    for n in (0, 2, 3):
        c._get(f"/movie/{n}")
    assert len(c.session.calls) == calls
    c._get("/movie/1")
    assert len(c.session.calls) == calls + 1


def test_cached_answer_expires_after_its_ttl(monkeypatch):
    c = client()
    c._get("/movie/1", ttl=60)
    c._get("/movie/1", ttl=60)
    assert len(c.session.calls) == 1
    later = time.monotonic() + 61
    monkeypatch.setattr(tmdb_module.time, "monotonic", lambda: later)
    c._get("/movie/1", ttl=60)
    assert len(c.session.calls) == 2


def test_requests_still_carry_the_key_and_language():
    c = client()
    c._get("/movie/1", page=2, genre=None)
    assert c.session.calls[0][1] == {
        "api_key": "secret-key",
        "language": "en-US",
        "page": 2,
    }


def test_saved_cache_is_used_after_a_restart(tmp_path):
    path = str(tmp_path / "tmdb.json")
    first = client()
    data = first._get("/movie/550", ttl=3600, append_to_response="credits")
    first._get("/genre/movie/list", ttl=86400)
    first.save(path)
    with open(path, encoding="utf-8") as f:
        assert "secret-key" not in f.read()

    second = client()
    assert second.load(path) == 2
    assert second._get("/movie/550", ttl=3600, append_to_response="credits") == data
    second._get("/genre/movie/list", ttl=86400)
    assert second.session.calls == []
    second._get("/movie/550")  # other params: not in the cache
    assert len(second.session.calls) == 1


def test_loaded_answers_keep_their_ttl(tmp_path, monkeypatch):
    path = str(tmp_path / "tmdb.json")
    first = client()
    first._get("/short", ttl=60)
    first._get("/long", ttl=3600)
    first.save(path)

    # Ten minutes later: the short answer is gone, the long one has 50 minutes left.
    wall = time.time() + 600
    monkeypatch.setattr(tmdb_module.time, "time", lambda: wall)
    second = client()
    assert second.load(path) == 1
    second._get("/long", ttl=3600)
    assert second.session.calls == []
    second._get("/short", ttl=60)
    assert len(second.session.calls) == 1
    soon = time.monotonic() + 3001
    monkeypatch.setattr(tmdb_module.time, "monotonic", lambda: soon)
    second._get("/long", ttl=3600)
    assert len(second.session.calls) == 2


def test_clock_behind_after_boot_doesnt_stretch_the_ttl(tmp_path, monkeypatch):
    path = str(tmp_path / "tmdb.json")
    first = client()
    first._get("/movie/1", ttl=60)
    first.save(path)
    behind = time.time() - 86400  # a Pi without a real-time clock, before NTP
    monkeypatch.setattr(tmdb_module.time, "time", lambda: behind)
    second = client()
    assert second.load(path) == 1
    later = time.monotonic() + 61
    monkeypatch.setattr(tmdb_module.time, "monotonic", lambda: later)
    second._get("/movie/1", ttl=60)
    assert len(second.session.calls) == 1


def test_cache_file_is_written_only_after_changes(tmp_path):
    path = tmp_path / "tmdb.json"
    c = client()
    c.save(str(path))
    assert not path.exists()
    c._get("/movie/1")
    c.save(str(path))
    stamp = path.stat().st_mtime_ns
    c._get("/movie/1")  # a cache hit changes nothing
    c.save(str(path))
    assert path.stat().st_mtime_ns == stamp
    c.clear_cache()  # a new API key: the file is emptied too
    c.save(str(path))
    assert json.loads(path.read_text(encoding="utf-8"))["entries"] == []


def test_broken_cache_file_is_ignored(tmp_path):
    path = tmp_path / "tmdb.json"
    path.write_text("{not json", encoding="utf-8")
    assert client().load(str(path)) == 0
    assert client().load(str(tmp_path / "missing.json")) == 0


def test_many_gives_the_same_rows_with_or_without_the_cache():
    rows = {
        "a": ("/movie/popular", {}),
        "b": ("/discover/movie", {"with_genres": 28, "sort_by": "popularity.desc"}),
        "broken": ("/down", {}),
    }
    c = client(fail={"/down"})
    cold = c.many(rows)
    calls = len(c.session.calls)
    warm = c.many(rows)
    assert warm == cold and list(warm) == list(rows)
    assert cold["broken"] == {"results": []}
    assert len(c.session.calls) == calls + 1  # only the failed row is asked again


def test_imdb_ids_match_one_lookup_per_title():
    items = [("movie", n) for n in (1, 2, 3, 4, 2)] + [("tv", 7)]
    c = client("cs-CZ")
    expected = {}
    for media_type, tmdb_id in items:
        imdb = c._get(f"/{media_type}/{tmdb_id}/external_ids", 7 * 86400).get("imdb_id")
        if imdb:
            expected[(media_type, tmdb_id)] = imdb
    fresh = client("cs-CZ")
    assert fresh.imdb_ids(items) == expected
    assert list(fresh.imdb_ids(items)) == list(expected)
    assert len(fresh.session.calls) == len(set(items))  # the second call: all cached
    # Asked in the server language, like the worker threads always did.
    assert {p["language"] for _, p in fresh.session.calls} == {"cs-CZ"}


def test_imdb_ids_survive_failures():
    c = client(fail={"/movie/5/external_ids"})
    assert c.imdb_ids([("movie", 5), ("movie", 4)]) == {("movie", 4): "tt0000004"}
    with pytest.raises(TMDBError):
        c._get("/movie/5/external_ids", 7 * 86400, "en-US")


# -- gzip, ETags -----------------------------------------------------------------------


def gzip_app():
    app = Flask(__name__)
    big = "<p>" + "StreamFinder " * 400 + "</p>"

    @app.route("/page")
    def page():
        return big

    @app.route("/small")
    def small():
        return "<p>hi</p>"

    @app.route("/json")
    def as_json():
        return {"items": [{"id": n, "title": f"Title {n}"} for n in range(200)]}

    @app.route("/stream")
    def stream():
        return Response((big for _ in range(3)), mimetype="text/html")

    @app.route("/video")
    def video():
        return Response(b"\0" * 5000, mimetype="video/mp4")

    @app.route("/encoded")
    def encoded():
        return Response(
            gzip.compress(big.encode()),
            mimetype="text/html",
            headers={"Content-Encoding": "gzip"},
        )

    compression.init_app(app)
    return app.test_client()


@pytest.mark.parametrize("path", ["/page", "/json"])
def test_gzip_only_when_accepted_and_the_body_round_trips(path):
    c = gzip_app()
    plain = c.get(path)
    packed = c.get(path, headers=GZIP)
    assert "Content-Encoding" not in plain.headers
    assert packed.headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(packed.data) == plain.data
    assert int(packed.headers["Content-Length"]) == len(packed.data) < len(plain.data)
    assert packed.headers["Content-Type"] == plain.headers["Content-Type"]
    assert "Accept-Encoding" in plain.headers["Vary"]
    assert "Accept-Encoding" in packed.headers["Vary"]
    refused = c.get(path, headers={"Accept-Encoding": "gzip;q=0, br"})
    assert "Content-Encoding" not in refused.headers


@pytest.mark.parametrize("path", ["/small", "/stream", "/video", "/encoded"])
def test_gzip_leaves_small_streamed_and_binary_responses_alone(path):
    c = gzip_app()
    plain = c.get(path)
    packed = c.get(path, headers=GZIP)
    assert packed.data == plain.data
    assert packed.headers.get("Content-Encoding") == plain.headers.get(
        "Content-Encoding"
    )


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TMDB_API_KEY", "test-key")
    monkeypatch.setattr(config, "SUBTITLE_CACHE_DIR", str(tmp_path / "subs"))
    return create_app(testing=True)


@pytest.mark.parametrize("asset", ["css/app.css", "js/app.js", "js/player.js"])
def test_static_files_gzip_and_answer_304(app, asset):
    c = app.test_client()
    url = f"/static/{asset}?v=1"
    with open(f"{app.static_folder}/{asset}", "rb") as f:
        original = f.read()
    plain = c.get(url)
    assert plain.data == original and "Content-Encoding" not in plain.headers
    for _ in range(2):  # the second one comes from the in-memory copy
        packed = c.get(url, headers=GZIP)
        assert packed.headers["Content-Encoding"] == "gzip"
        assert gzip.decompress(packed.data) == original
        assert packed.headers["Cache-Control"] == plain.headers["Cache-Control"]
    strong, weak = plain.headers["ETag"], packed.headers["ETag"]
    assert weak == f"W/{strong}"
    assert c.get(url, headers={"If-None-Match": strong}).status_code == 304
    again = c.get(url, headers={**GZIP, "If-None-Match": weak})
    assert again.status_code == 304 and again.data == b""
    assert c.get(url, headers={**GZIP, "If-None-Match": '"other"'}).status_code == 200
    ranged = c.get(url, headers={"Range": "bytes=0-9"})
    assert ranged.status_code == 206 and ranged.data == original[:10]


def test_subtitles_answer_304_when_unchanged(app, monkeypatch):
    vtt = "WEBVTT\n\n" + "".join(
        f"00:00:{n:02d}.000 --> 00:00:{n:02d}.500\nLine {n}\n\n" for n in range(60)
    )
    svc = app.extensions["streamfinder"]
    monkeypatch.setattr(svc.subtitles, "get_vtt", lambda candidate: vtt)
    c = app.test_client()
    url = "/api/subtitles/org-123.vtt"
    first = c.get(url)
    assert first.status_code == 200 and first.get_data(as_text=True) == vtt
    assert first.headers["Cache-Control"] == "private, max-age=86400"
    etag = first.headers["ETag"]
    assert c.get(url, headers={"If-None-Match": etag}).status_code == 304
    packed = c.get(url, headers=GZIP)
    assert gzip.decompress(packed.data).decode() == vtt
    assert (
        c.get(
            url, headers={**GZIP, "If-None-Match": packed.headers["ETag"]}
        ).status_code
        == 304
    )


# -- pages -----------------------------------------------------------------------------


@pytest.mark.parametrize("lang", ["en", "cs"])
def test_cached_i18n_json_is_what_tojson_gave(app, lang):
    with app.test_request_context(headers={"Cookie": f"lang={lang}"}):
        before = render_template_string("{{ js_strings() | tojson }}")
        assert render_template_string("{{ js_strings_json() }}") == before
        assert json.loads(before) == i18n.js_strings()


def test_page_embeds_the_same_strings(app, monkeypatch):
    svc = app.extensions["streamfinder"]
    monkeypatch.setattr(svc.tmdb, "_get", lambda *a, **k: {"results": []})
    c = app.test_client()
    for lang in ("en", "cs"):
        c.set_cookie("lang", lang)
        html = c.get("/").get_data(as_text=True)
        found = re.search(
            r'<script id="i18n" type="application/json">(.*?)</script>', html
        )
        with app.test_request_context(headers={"Cookie": f"lang={lang}"}):
            assert json.loads(found.group(1)) == i18n.js_strings()
            assert found.group(1) == render_template_string(
                "{{ js_strings() | tojson }}"
            )


# -- subtitles ---------------------------------------------------------------------------


def _old_clean_text(text: str) -> str:
    """`_clean_text` before the quick checks and precompiled patterns."""
    text = re.sub(r"\{\\[^}]*\}", "", text)
    text = re.sub(r"\\N|\\n", "\n", text)
    text = re.sub(r"<font[^>]*>|</font>", "", text, flags=re.IGNORECASE)
    parts = []
    last = 0
    for match in formats._ALLOWED_TAG_RE.finditer(text):
        parts.append(
            text[last : match.start()]
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        parts.append(match.group(0).lower())
        last = match.end()
    parts.append(
        text[last:].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    text = "".join(parts).replace("--&gt;", "→")
    return "\n".join(line.strip() for line in text.strip().splitlines() if line.strip())


def _old_timed_cues(text: str) -> list[tuple]:
    """The SRT / WebVTT part of `parse_cues` before timing lines were pre-checked."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    cues, lines = [], text.split("\n")
    time_re = (
        formats._VTT_TIME_RE if text.startswith("WEBVTT") else formats._SRT_TIME_RE
    )
    i = 0
    while i < len(lines):
        match = time_re.search(lines[i])
        if not match:
            i += 1
            continue
        g = match.groups()
        start, end = formats._ms(*g[:4]), formats._ms(*g[4:])
        i += 1
        body = []
        while i < len(lines) and lines[i].strip():
            if time_re.search(lines[i]):
                break
            body.append(lines[i])
            i += 1
        if (
            body
            and body[-1].strip().isdigit()
            and i < len(lines)
            and time_re.search(lines[i])
        ):
            body.pop()
        cues.append((start, end, "\n".join(body)))
    return cues


_PIECES = [
    "Ahoj",
    "a & b",
    "<i>kurzíva</i>",
    "<I>VELKÁ</I>",
    "<font color=red>barva</font>",
    "<b>tučně",
    "{\\an8}nahoře",
    "řádek\\Ndruhý",
    "x\\ny",
    "1 < 2 > 0",
    "--> šipka",
    "-- > ne",
    "{bez lomítka}",
    "  mezery  ",
    "<script>",
    "",
    "42",
    "www.example.com",
]


def _random_text(rng: random.Random) -> str:
    return rng.choice([" ", "\n", "\\N", ""]).join(
        rng.choice(_PIECES) for _ in range(rng.randint(1, 4))
    )


def test_clean_text_is_unchanged():
    rng = random.Random(11)
    for _ in range(3000):
        text = _random_text(rng)
        assert formats._clean_text(text) == _old_clean_text(text), text


def _random_subtitle(rng: random.Random, vtt: bool) -> str:
    sep = "." if vtt else ","
    out = ["WEBVTT", ""] if vtt else []
    t = 0
    for n in range(rng.randint(1, 40)):
        start, t = t, t + rng.randint(500, 4000)
        if not vtt or rng.random() < 0.3:
            out.append(str(n + 1))
        s = f"00:{start // 60000 % 60:02d}:{start // 1000 % 60:02d}{sep}{start % 1000:03d}"
        e = f"00:{t // 60000 % 60:02d}:{t // 1000 % 60:02d}{sep}{t % 1000:03d}"
        out.append(f"{s} --> {e}" + (" align:start" if rng.random() < 0.2 else ""))
        out += [_random_text(rng).replace("\n", " ") for _ in range(rng.randint(0, 3))]
        if rng.random() < 0.2:
            out.append(str(n + 2))  # a counter glued to the text, no blank line
        elif rng.random() < 0.9:
            out.append("")
    return rng.choice(["\n", "\r\n"]).join(out)


def test_subtitle_parsing_and_webvtt_are_unchanged():
    rng = random.Random(5)
    for n in range(600):
        text = _random_subtitle(rng, vtt=n % 3 == 0)
        old = _old_timed_cues(text)
        assert formats.parse_cues(text) == old
        expected = ["WEBVTT", ""]
        for start, end, body in old:
            cleaned = _old_clean_text(body)
            if cleaned and end > start and not formats.is_advert(cleaned):
                expected += [
                    f"{formats._vtt_time(start)} --> {formats._vtt_time(end)}",
                    cleaned,
                    "",
                ]
        assert formats.to_webvtt(text) == "\n".join(expected)


def test_ranking_scores_each_release_name_once(monkeypatch):
    from streamfinder.matching import parse_release, release_match_score
    from streamfinder.subtitles import service
    from streamfinder.subtitles.opensubtitles_org import OrgSubtitle

    releases = ["Film.2014.1080p.BluRay.x264-SPARKS", "Film.2014.720p.WEB-DL-NTb"]
    api = [
        {
            "attributes": {
                "language": lang,
                "files": [{"file_id": n * 10 + i}],
                "release": release,
                "download_count": n * 100,
            }
        }
        for n, release in enumerate(releases)
        for i, lang in enumerate(["cs", "en"])
    ]
    org = [
        OrgSubtitle(900 + n, "sk", releases[n % 2], 50, None, 1, "srt")
        for n in range(4)
    ]
    video = parse_release("Film.2014.1080p.BluRay.x264-SPARKS.mkv")
    calls = []

    def counted(release, info):
        calls.append(release)
        return release_match_score(release, info)

    monkeypatch.setattr(service, "release_match_score", counted)
    merged = SubtitleService._merge_online(api, org, video, ["cs", "sk", "en"])
    assert sorted(calls) == sorted(releases)
    by_id = {c.id: c for c in merged}
    for sub in org:
        expected = release_match_score(sub.release, video) + service._popularity(50)
        assert by_id[f"org-{sub.id_subtitle}"].score == round(expected, 1)
    for item in api:
        attrs = item["attributes"]
        expected = release_match_score(attrs["release"], video) + service._popularity(
            attrs["download_count"]
        )
        assert by_id[f"os-{attrs['files'][0]['file_id']}"].score == round(expected, 1)
