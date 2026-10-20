"""Benchmark of the hot paths, with synthetic data and no network.

    python tests/bench.py            # table of timings and response sizes
    python tests/bench.py --json     # the same as JSON (to compare two runs)

Not collected by pytest (no test_ prefix). Times are the median of several runs,
in milliseconds per call; sizes are the bytes a phone downloads.
"""

import json
import os
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from contextlib import closing

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TMDB_API_KEY", "bench")

RESULTS: dict[str, float] = {}


def timed(name: str, fn, repeat: int = 20, warmup: int = 2) -> float:
    for _ in range(warmup):
        fn()
    runs = []
    for _ in range(repeat):
        started = time.perf_counter()
        fn()
        runs.append((time.perf_counter() - started) * 1000)
    RESULTS[name] = round(statistics.median(runs), 3)
    return RESULTS[name]


def size(name: str, value: float) -> None:
    RESULTS[name] = value


# -- synthetic TMDB ------------------------------------------------------------------

_WORDS = [
    "the",
    "dark",
    "night",
    "return",
    "of",
    "king",
    "lost",
    "city",
    "dream",
    "shadow",
    "river",
    "fire",
    "last",
    "star",
]


def _title(rng: random.Random) -> str:
    return " ".join(rng.choice(_WORDS) for _ in range(rng.randint(1, 4))).title()


def _list_item(rng: random.Random, media_type: str, i: int) -> dict:
    """A TMDB list result with every field TMDB sends."""
    title = _title(rng)
    item = {
        "adult": False,
        "backdrop_path": f"/b{i}abcdefghijklmnop.jpg",
        "genre_ids": [28, 12, 878],
        "id": 1000 + i,
        "original_language": "en",
        "overview": "A long overview of the plot. " * 8,
        "popularity": rng.uniform(1, 500),
        "poster_path": f"/p{i}abcdefghijklmnop.jpg",
        "vote_average": rng.uniform(4, 9),
        "vote_count": rng.randint(10, 30000),
        "media_type": media_type,
    }
    if media_type == "movie":
        item.update(
            title=title, original_title=title, release_date="2014-11-05", video=False
        )
    else:
        item.update(
            name=title,
            original_name=title,
            first_air_date="2011-04-17",
            origin_country=["US"],
        )
    return item


def _fake_tmdb_payload(path: str, params: dict) -> dict:
    rng = random.Random(hash((path, params.get("page"), params.get("query"))) & 0xFFFF)
    if path.startswith("/genre/"):
        return {"genres": [{"id": 10 + i, "name": f"Genre {i}"} for i in range(19)]}
    if path.endswith("/external_ids"):
        return {"imdb_id": f"tt{int(path.split('/')[2]):07d}"}
    if "/season/" in path:
        return {
            "episodes": [
                {
                    "episode_number": e,
                    "name": f"Episode {e}",
                    "overview": "What happens. " * 10,
                    "runtime": 55,
                    "air_date": "2011-04-17",
                    "still_path": f"/s{e}.jpg",
                }
                for e in range(1, 11)
            ]
        }
    if path.startswith(("/movie/", "/tv/")) and path.split("/")[2].isdigit():
        media_type = path.split("/")[1]
        data = _list_item(rng, media_type, int(path.split("/")[2]))
        data.update(
            runtime=169,
            tagline="Mankind was born on Earth.",
            genres=[{"id": 1, "name": "Drama"}, {"id": 2, "name": "Sci-Fi"}],
            imdb_id="tt0816692",
            external_ids={"imdb_id": "tt0816692"},
            credits={
                "cast": [
                    {
                        "name": f"Actor {c}",
                        "character": f"Role {c}",
                        "profile_path": f"/a{c}.jpg",
                    }
                    for c in range(60)
                ],
                "crew": [{"name": f"Crew {c}", "job": "Grip"} for c in range(150)],
            },
            videos={
                "results": [
                    {
                        "site": "YouTube",
                        "type": "Trailer",
                        "official": True,
                        "iso_639_1": "en",
                        "key": f"k{v}",
                    }
                    for v in range(8)
                ]
            },
            recommendations={
                "results": [_list_item(rng, media_type, 5000 + r) for r in range(20)]
            },
            alternative_titles={"titles": [{"iso_3166_1": "US", "title": "Alt"}] * 10},
            seasons=[
                {"season_number": s, "name": f"Season {s}", "episode_count": 10}
                for s in range(6)
            ],
        )
        return data
    media_type = "tv" if "/tv" in path else "movie"
    results = [
        _list_item(
            rng,
            rng.choice(["movie", "tv", "person"])
            if "multi" in path or "trending" in path
            else media_type,
            i,
        )
        for i in range(20)
    ]
    for r in results:
        if r["media_type"] == "person":
            r.update(
                name="Christopher Nolan", known_for=[_list_item(rng, "movie", 9000)]
            )
    return {
        "page": params.get("page", 1),
        "results": results,
        "total_pages": 50,
        "total_results": 1000,
    }


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return json.loads(json.dumps(self._data))  # a fresh object, like requests


def _fake_get(url, params=None, timeout=None):
    path = url.split("/3", 1)[1]
    return _FakeResponse(_fake_tmdb_payload(path, params or {}))


# -- benchmarks ---------------------------------------------------------------------


def bench_startup():
    from streamfinder import create_app

    started = time.perf_counter()
    app = create_app(testing=True)
    created = (time.perf_counter() - started) * 1000
    client = app.test_client()
    started = time.perf_counter()
    client.get("/api/healthz")
    RESULTS["startup.create_app"] = round(created, 1)
    RESULTS["startup.first_request"] = round((time.perf_counter() - started) * 1000, 1)


def bench_pages(cache_dir: str):
    from streamfinder import config, create_app
    from streamfinder.tmdb import tmdb_client

    config.TMDB_API_KEY = tmdb_client.api_key = "bench"
    tmdb_client.session.get = _fake_get
    tmdb_client.clear_cache()
    app = create_app(testing=True)
    svc = app.extensions["streamfinder"]
    svc.imdb.path = os.path.join(cache_dir, "missing.sqlite")
    client = app.test_client()
    gz = {"Accept-Encoding": "gzip"}
    pages = {
        "home": "/",
        "browse": "/movies?genre=28",
        "detail_movie": "/movie/157336",
        "detail_show": "/show/1399",
        "search_page": "/search?q=nolan",
        "api_search": "/api/search?q=nolan",
        "api_browse": "/api/browse?type=movie&page=2",
        "api_season": "/api/season/1399/1",
    }
    for name, url in pages.items():
        timed(f"page.{name}", lambda url=url: client.get(url), repeat=30)
        # What a phone gets: the same page, gzipped when the server can.
        timed(
            f"page.{name}_gzip", lambda url=url: client.get(url, headers=gz), repeat=30
        )
        plain = client.get(url)
        assert plain.status_code == 200, (url, plain.status_code)
        size(f"bytes.{name}", len(client.get(url, headers=gz).data))

    # A cold TMDB cache: every page re-fetches (what a restart costs).
    def cold():
        tmdb_client.clear_cache()
        client.get("/")

    timed("page.home_cold_cache", cold, repeat=10)

    # A restart (update, power cut): TMDB requests needed to show the same pages again.
    calls = []

    def counting(url, params=None, timeout=None):
        calls.append(url)
        return _fake_get(url, params, timeout)

    tmdb_client.session.get = counting
    for url in pages.values():
        client.get(url)
    saved = os.path.join(cache_dir, "tmdb.json")
    if hasattr(tmdb_client, "save"):  # the disk cache (not in older versions)
        tmdb_client.save(saved)
        size("bytes.tmdb_cache_file", os.path.getsize(saved))
    tmdb_client.clear_cache()
    if hasattr(tmdb_client, "load"):
        tmdb_client.load(saved)
    calls.clear()
    for url in pages.values():
        client.get(url)
    size("tmdb_requests_after_restart", len(calls))
    tmdb_client.session.get = _fake_get
    html = client.get("/").get_data(as_text=True)
    start = html.index('<script id="i18n"')
    size("bytes.i18n_json_in_page", len(html[start : html.index("</script>", start)]))
    player_dir = os.path.join(app.static_folder, "js", "player")
    player = sorted(f"js/player/{name}" for name in os.listdir(player_dir))
    for asset in ("css/app.css", "js/app.js", *player):
        size(
            f"bytes.static.{asset}",
            len(client.get(f"/static/{asset}", headers=gz).data),
        )


def _srt(cues: int, plain: bool = False) -> str:
    rng = random.Random(5)
    out, t = [], 60.0
    for i in range(cues):
        if plain and i % 8:  # most lines of a real film: no tags, no ampersands
            start, end = t, t + rng.uniform(1, 4)
            out.append(
                f"{i + 1}\n{_srt_time(start)} --> {_srt_time(end)}\nLine {i} of the film\n{_title(rng)}, more\n"
            )
            t = end + rng.uniform(0.3, 3)
            continue
        length = rng.uniform(1, 4)
        start, end = t, t + length
        out.append(
            f"{i + 1}\n{_srt_time(start)} --> {_srt_time(end)}\n<i>Line {i} of the film</i>\n{_title(rng)} & more\n"
        )
        t = end + rng.uniform(0.3, 3)
    return "\n".join(out)


def _srt_time(t: float) -> str:
    ms = int(t * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _ass(cues: int) -> str:
    rng = random.Random(6)
    head = "[Script Info]\nTitle: x\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    rows, t = [], 60.0
    for i in range(cues):
        s, e = t, t + rng.uniform(1, 4)
        rows.append(
            f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Default,,0,0,0,,{{\\an8}}Line {i}\\N{_title(rng)}"
        )
        t = e + rng.uniform(0.3, 3)
    return head + "\n".join(rows)


def _ass_time(t: float) -> str:
    cs = int(t * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def bench_subtitles():
    from streamfinder.subtitles.formats import decode_subtitle, parse_cues, to_webvtt

    srt, ass = _srt(1500), _ass(1500)  # about two hours of dialogue
    raw = srt.encode("cp1250")
    timed("subs.parse_srt", lambda: parse_cues(srt), repeat=15)
    timed("subs.to_webvtt_srt", lambda: to_webvtt(srt), repeat=15)
    plain = _srt(1500, plain=True)
    timed("subs.to_webvtt_srt_mostly_plain", lambda: to_webvtt(plain), repeat=15)
    timed("subs.to_webvtt_ass", lambda: to_webvtt(ass), repeat=15)
    timed("subs.decode_cp1250", lambda: decode_subtitle(raw, "cs"), repeat=15)


def bench_ranking(cache_dir: str):
    from streamfinder.matching import parse_release
    from streamfinder.subtitles.opensubtitles_org import OrgSubtitle
    from streamfinder.subtitles.service import SubtitleService

    rng = random.Random(9)
    groups = ["SPARKS", "YIFY", "RARBG", "FGT", "NTb", "EVO", "playSD"]

    def release():
        return f"Interstellar.2014.{rng.choice(['1080p', '720p', '2160p'])}.{rng.choice(['BluRay', 'WEB-DL', 'HDRip'])}.x264-{rng.choice(groups)}{rng.randint(0, 99)}"

    api_items = [
        {
            "attributes": {
                "language": rng.choice(["cs", "sk", "en"]),
                "legacy_subtitle_id": rng.randint(1, 10**6) if i % 2 else None,
                "files": [{"file_id": i, "file_name": release()}],
                "release": release(),
                "download_count": rng.randint(0, 50000),
                "moviehash_match": i == 3,
                "from_trusted": i % 5 == 0,
            }
        }
        for i in range(300)
    ]
    org_items = [
        OrgSubtitle(
            id_subtitle=rng.randint(1, 10**6),
            language=rng.choice(["cs", "sk", "en"]),
            release=release(),
            downloads=rng.randint(0, 50000),
            cds=1,
            fps=23.976,
            format="srt",
        )
        for _ in range(300)
    ]
    video = parse_release("Interstellar.2014.1080p.BluRay.x264-SPARKS.mkv")
    timed(
        "subs.rank_600_candidates",
        lambda: SubtitleService._merge_online(
            api_items, org_items, video, ["cs", "sk", "en"]
        ),
        repeat=10,
    )


def bench_sync():
    from streamfinder.subtitles.sync import align, align_text

    rng = random.Random(2)
    speech, t = [], 3000.0
    while t < 3120:
        length = rng.uniform(0.8, 3.5)
        speech.append((t, t + length))
        t += length + rng.uniform(0.3, 2.5)
    film, t = [], 0.0
    while t < 7200:
        length = rng.uniform(0.8, 3.5)
        film.append((t, t + length))
        t += length + rng.uniform(0.3, 2.5)
    cues = film + [(s - 2.0, e - 2.0) for s, e in speech]
    timed("sync.align_full_film", lambda: align(cues, speech, (3000, 3120)), repeat=5)

    words = [
        "we",
        "have",
        "to",
        "go",
        "back",
        "there",
        "is",
        "no",
        "time",
        "left",
        "for",
        "anything",
        "else",
        "now",
    ]
    text_cues = [
        (s, e, " ".join(rng.choice(words) for _ in range(6))) for s, e in sorted(cues)
    ]
    # A transcript of 150 s around 50 minutes, 1.4 s late.
    lines = [(s + 1.4, e + 1.4, x) for s, e, x in text_cues if 3000 <= s < 3150]
    timed(
        "sync.align_text_full_film",
        lambda: align_text(text_cues, lines),
        repeat=3,
        warmup=1,
    )


def bench_imdb(cache_dir: str):
    from streamfinder.imdb import IMDbRatings

    path = os.path.join(cache_dir, "imdb.sqlite")
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating REAL, votes INTEGER) WITHOUT ROWID"
        )
        db.executemany(
            "INSERT INTO ratings VALUES (?, ?, ?)",
            ((i, 7.5, i) for i in range(1, 300_000)),
        )
        db.commit()
    imdb = IMDbRatings(path)
    ids = [
        f"tt{i:07d}" for i in range(1000, 300_000, 15_000)
    ]  # a poster row: 20 titles
    timed("imdb.get", lambda: imdb.get("tt0123456"), repeat=200)
    timed("imdb.many_20", lambda: imdb.many(ids), repeat=200)
    timed("imdb.ready", lambda: imdb.ready, repeat=200)


def bench_json():
    from flask import Flask, jsonify

    app = Flask(__name__)
    rng = random.Random(4)
    items = [_list_item(rng, "movie", i) for i in range(500)]
    with app.app_context():
        timed(
            "json.jsonify_500_items", lambda: jsonify(items=items).get_data(), repeat=30
        )


def main():
    with tempfile.TemporaryDirectory() as cache_dir:
        os.environ.setdefault("SUBTITLE_CACHE_DIR", os.path.join(cache_dir, "subs"))
        os.environ.setdefault("IMDB_DB_PATH", os.path.join(cache_dir, "missing.sqlite"))
        os.environ.setdefault("TMDB_CACHE_FILE", os.path.join(cache_dir, "tmdb.json"))
        import logging

        logging.disable(logging.WARNING)
        bench_startup()
        bench_pages(cache_dir)
        bench_subtitles()
        bench_ranking(cache_dir)
        bench_sync()
        bench_imdb(cache_dir)
        bench_json()
    if "--json" in sys.argv:
        print(json.dumps(RESULTS, indent=1))
        return
    before = {}
    if "--compare" in sys.argv:  # --compare before.json: a column with the old numbers
        with open(sys.argv[sys.argv.index("--compare") + 1], encoding="utf-8") as f:
            before = json.load(f)
    width = max(map(len, RESULTS))
    for name, value in RESULTS.items():
        unit = "B" if name.startswith("bytes.") else "" if "requests" in name else "ms"
        line = f"{name:<{width}}  {_number(value, unit)}"
        if before:
            old = before.get(name)
            line = f"{name:<{width}}  {_number(old, unit) if old is not None else '-':>15}  ->  {_number(value, unit)}"
        print(line)


def _number(value: float, unit: str) -> str:
    text = f"{value:,.3f}" if unit == "ms" else f"{value:,}"
    return f"{text:>12} {unit}".rstrip()


if __name__ == "__main__":
    main()
