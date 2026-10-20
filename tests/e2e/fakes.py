"""Stand-ins for everything outside the app: TMDB, TorrServer, torrent indexes, media.

The app itself runs unchanged; only its service objects are pointed at these.
"""

import copy
import datetime
import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from streamfinder.tmdb import TMDBClient
from streamfinder.torrents.providers import RawTorrent

# -- media -------------------------------------------------------------------------

DURATION = 20  # seconds of every test video


def ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # not installed, or no binary for this platform
        return None


def make_video(ffmpeg: str, path: str, languages: list[str]) -> None:
    """A small H.264 + AAC MP4 with one sine-tone audio track per language.

    A keyframe every second, so seeking in a converted stream lands close to the target.
    """
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    cmd += ["-f", "lavfi", "-i", f"testsrc=size=320x180:rate=15:duration={DURATION}"]
    for n, _ in enumerate(languages):
        cmd += [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={440 + 220 * n}:duration={DURATION}",
        ]
    cmd += ["-map", "0:v"] + [
        x for n in range(len(languages)) for x in ("-map", f"{n + 1}:a")
    ]
    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-g",
        "15",
        "-pix_fmt",
        "yuv420p",
    ]
    cmd += ["-c:a", "aac", "-b:a", "48k"]
    for n, language in enumerate(languages):
        cmd += [f"-metadata:s:a:{n}", f"language={language}"]
    cmd += ["-movflags", "+faststart", path]
    subprocess.run(cmd, check=True, timeout=60)


def srt(language: str) -> str:
    """One line per second: "<language> line <n>" from n s to n + 0.9 s."""
    blocks = []
    for n in range(DURATION):
        blocks.append(
            f"{n + 1}\n00:00:{n:02d},000 --> 00:00:{n:02d},900\n{language} line {n}\n"
        )
    return "\n".join(blocks)


# -- catalogue ---------------------------------------------------------------------

MOVIE_ID, FUTURE_ID, SHOW_ID, UPCOMING_ID = 100, 101, 300, 301
MOVIE_HASH = "a1" * 20
SHOW_HASH = "b2" * 20
MOVIE_NAME = (
    "Test.Movie.2020.1080p.WEB-DL.x264.AAC-GRP"  # browser-safe: played directly
)
SHOW_NAME = "Test.Show.S01.1080p.WEB-DL.x264-GRP"  # audio unknown: probed, 2 tracks
FUTURE_DATE = "2027-12-18"


def _list_item(tmdb_id, media_type, title, date, rating=7.5):
    key = "title" if media_type == "movie" else "name"
    date_key = "release_date" if media_type == "movie" else "first_air_date"
    return {
        "id": tmdb_id,
        "media_type": media_type,
        key: title,
        date_key: date,
        "vote_average": rating,
        "poster_path": f"/poster{tmdb_id}.jpg",
        "backdrop_path": f"/backdrop{tmdb_id}.jpg",
        "overview": f"{title} is a film made for tests.",
        "popularity": 100.0,
        "vote_count": 1000,
    }


def tmdb_data(today: datetime.date | None = None) -> dict[str, dict]:
    """Canned TMDB answers by API path."""
    today = today or datetime.date.today()
    day = lambda n: (today + datetime.timedelta(days=n)).isoformat()
    movie = _list_item(MOVIE_ID, "movie", "Test Movie", "2020-05-01", 7.8)
    future = _list_item(FUTURE_ID, "movie", "Future Film", FUTURE_DATE, 0)
    show = _list_item(SHOW_ID, "tv", "Test Show", "2020-01-10", 8.1)
    movies = {"results": [movie, future], "page": 1, "total_pages": 1}
    shows = {"results": [show], "page": 1, "total_pages": 1}
    episodes = [
        {
            "episode_number": n,
            "name": f"Episode {n}",
            "overview": f"Episode {n} of the test show.",
            "air_date": f"2020-01-{9 + n:02d}",
            "runtime": 1,
            "still_path": f"/still{n}.jpg",
        }
        for n in (1, 2, 3)
    ]
    detail = lambda item, **extra: {
        **item,
        "genres": [{"id": 18, "name": "Drama"}],
        "credits": {
            "cast": [{"name": "Ann Actor", "character": "Lead", "profile_path": None}]
        },
        "videos": {"results": []},
        "recommendations": {"results": []},
        "alternative_titles": {"titles": []},
        "external_ids": {},
        **extra,
    }
    return {
        "/trending/all/week": {
            "results": [movie, show, future],
            "page": 1,
            "total_pages": 1,
        },
        "/movie/popular": movies,
        "/movie/top_rated": movies,
        "/tv/popular": shows,
        "/tv/top_rated": shows,
        "/discover/movie": movies,
        "/discover/tv": shows,
        "/genre/movie/list": {"genres": [{"id": 18, "name": "Drama"}]},
        "/genre/tv/list": {"genres": [{"id": 18, "name": "Drama"}]},
        f"/movie/{MOVIE_ID}": detail(
            movie, status="Released", runtime=None, original_language="en"
        ),
        f"/movie/{FUTURE_ID}": detail(future, status="In Production", runtime=None),
        f"/tv/{SHOW_ID}": detail(
            show,
            status="Ended",
            original_language="en",
            episode_run_time=[1],
            number_of_episodes=3,
            seasons=[{"season_number": 1, "name": "Season 1", "episode_count": 3}],
            last_episode_to_air={
                "season_number": 1,
                "episode_number": 3,
                "air_date": "2020-01-12",
            },
        ),
        f"/tv/{SHOW_ID}/season/1": {"season_number": 1, "episodes": episodes},
        f"/tv/{UPCOMING_ID}": detail(
            _list_item(UPCOMING_ID, "tv", "Upcoming Show", "2021-03-01"),
            status="Returning Series",
            next_episode_to_air={
                "season_number": 2,
                "episode_number": 1,
                "air_date": day(1),
            },
            seasons=[
                {"season_number": 1, "name": "Season 1", "episode_count": 2},
                {"season_number": 2, "name": "Season 2", "episode_count": 3},
            ],
        ),
        f"/tv/{UPCOMING_ID}/season/2": {
            "season_number": 2,
            "episodes": [
                {"episode_number": 1, "name": "Return", "air_date": day(1)},
                {"episode_number": 2, "name": "Same Night", "air_date": day(1)},
                {"episode_number": 3, "name": "Later", "air_date": day(5)},
            ],
        },
    }


class FakeTMDB(TMDBClient):
    """The real client with its HTTP layer replaced by the canned answers."""

    def __init__(self, data: dict[str, dict]):
        super().__init__("test")
        self.data = data
        self.paths: list[str] = []

    def _get(self, path, ttl=3600, language=None, **params):
        self.paths.append(path)
        return copy.deepcopy(
            self.data.get(path, {"results": [], "page": 1, "total_pages": 1})
        )

    def check_key(self, api_key):
        return api_key == "test"


class FakeProvider:
    """One torrent index that always knows the test releases."""

    name = "FakeIndex"
    media_types = ("movie", "tv")
    mirrors = ("http://fake-index.invalid",)

    def search(self, query) -> list[RawTorrent]:
        if query.media_type == "movie":
            return [
                RawTorrent(MOVIE_NAME, MOVIE_HASH, 1_500_000_000, 150, 10, self.name)
            ]
        return [RawTorrent(SHOW_NAME, SHOW_HASH, 3_000_000_000, 120, 8, self.name)]

    def is_available(self) -> bool:
        return True


# -- TorrServer --------------------------------------------------------------------


class FakeTorrServer:
    """The TorrServer endpoints the app uses, serving real files with HTTP Range."""

    def __init__(self, catalogue: dict[str, dict]):
        # hash -> {"title", "files": [(id, path, local file)]}
        self.catalogue = catalogue
        self.added: dict[str, dict] = {}
        self.dropped: list[str] = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def reset(self):
        with self.lock:
            self.added.clear()
            self.dropped.clear()

    def status(self, info_hash: str) -> dict | None:
        torrent = self.catalogue.get(info_hash)
        if torrent is None or info_hash not in self.added:
            return None
        return {
            "hash": info_hash,
            "title": self.added[info_hash].get("title") or torrent["title"],
            "poster": self.added[info_hash].get("poster", ""),
            "stat": 3,
            "torrent_size": sum(os.path.getsize(f) for _, _, f in torrent["files"]),
            "timestamp": 1_700_000_000,
            "connected_seeders": 12,
            "active_peers": 20,
            "download_speed": 4_000_000,
            "file_stats": [
                {"id": i, "path": path, "length": os.path.getsize(f)}
                for i, path, f in torrent["files"]
            ],
        }

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _send(self, status, body=b"", content_type="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, data, status=200):
                self._send(status, json.dumps(data).encode())

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                path = urlparse(self.path).path
                if path == "/settings":
                    return self._json({})
                if path != "/torrents":
                    return self._send(404, b"not found", "text/plain")
                action, info_hash = body.get("action"), (body.get("hash") or "").lower()
                with fake.lock:
                    if action == "add":
                        match = re.search(
                            r"btih:([a-fA-F0-9]{40})", body.get("link", "")
                        )
                        info_hash = match.group(1).lower() if match else ""
                        if info_hash not in fake.catalogue:
                            return self._send(400, b"unknown torrent", "text/plain")
                        fake.added[info_hash] = {
                            "title": body.get("title"),
                            "poster": body.get("poster"),
                        }
                        return self._json(fake.status(info_hash))
                    if action == "get":
                        status = fake.status(info_hash)
                        return (
                            self._json(status)
                            if status
                            else self._send(404, b"", "text/plain")
                        )
                    if action == "list":
                        return self._json([fake.status(h) for h in fake.added])
                    if action in ("drop", "rem"):
                        fake.dropped.append(info_hash)
                        if action == "rem":
                            fake.added.pop(info_hash, None)
                        return self._send(200, b"", "text/plain")
                return self._send(400, b"unknown action", "text/plain")

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/echo":
                    return self._send(200, b"MatriX.136", "text/plain")
                if not parsed.path.startswith("/stream"):
                    return self._send(404, b"not found", "text/plain")
                query = parse_qs(parsed.query)
                info_hash = (query.get("link") or [""])[0].lower()
                index = int((query.get("index") or ["-1"])[0])
                torrent = fake.catalogue.get(info_hash) or {"files": []}
                local = next((f for i, _, f in torrent["files"] if i == index), None)
                if local is None:
                    return self._send(404, b"no such file", "text/plain")
                self._serve_file(local)

            do_HEAD = do_GET

            def _serve_file(self, local):
                size = os.path.getsize(local)
                start, end = 0, size - 1
                match = re.fullmatch(
                    r"bytes=(\d*)-(\d*)", self.headers.get("Range") or ""
                )
                if match:
                    if match.group(1):
                        start = int(match.group(1))
                        end = int(match.group(2)) if match.group(2) else size - 1
                    else:  # the last N bytes
                        start = max(0, size - int(match.group(2)))
                    end = min(end, size - 1)
                    if start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                content_type = (
                    "video/mp4"
                    if local.endswith(".mp4")
                    else "application/octet-stream"
                )
                self.send_response(206 if match else 200)
                self.send_header("Content-Type", content_type)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                if match:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if self.command == "HEAD":
                    return
                try:
                    with open(local, "rb") as f:
                        f.seek(start)
                        remaining = end - start + 1
                        while remaining > 0:
                            chunk = f.read(min(65536, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # the browser stops reading when it has enough (or seeks)

        return Handler
