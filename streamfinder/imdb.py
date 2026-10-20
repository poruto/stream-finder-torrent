"""IMDb ratings from the official daily dataset (https://datasets.imdbws.com).

TMDB has no IMDb ratings, and scraping IMDb is fragile, so the ~9 MB
title.ratings.tsv.gz is downloaded once a day into a small SQLite table and
looked up locally. Until the first download finishes, lookups return nothing.
"""

import gzip
import logging
import os
import sqlite3
import threading
import time
from contextlib import closing

import requests

logger = logging.getLogger(__name__)

DATASET_URL = "https://datasets.imdbws.com/title.ratings.tsv.gz"
MAX_AGE = 24 * 3600


class IMDbRatings:
    def __init__(self, path: str):
        self.path = path
        self._count = 0
        self._started = False
        self._lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return os.path.exists(self.path)

    @property
    def count(self) -> int:
        if not self._count and self.ready:
            with closing(self._connect()) as db:
                self._count = db.execute("SELECT COUNT(*) FROM ratings").fetchone()[0]
        return self._count

    def start(self) -> None:
        """Refresh in a background thread now and then once a day."""
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._loop, name="imdb-ratings", daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                if (
                    not self.ready
                    or time.time() - os.path.getmtime(self.path) > MAX_AGE
                ):
                    self.refresh()
            except Exception as e:  # network or disk problems: retry later
                logger.warning("IMDb ratings refresh failed: %s", e)
            time.sleep(3600)

    def refresh(self) -> None:
        started = time.monotonic()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp_path = f"{self.path}.{os.getpid()}.tmp"
        try:
            with requests.get(DATASET_URL, stream=True, timeout=60) as response:
                response.raise_for_status()
                response.raw.decode_content = True
                # closing(): sqlite's own context manager commits but leaves the file open,
                # and an open file can't be replaced on Windows.
                with (
                    gzip.GzipFile(fileobj=response.raw) as data,
                    closing(sqlite3.connect(tmp_path)) as db,
                ):
                    db.execute(
                        "CREATE TABLE ratings (id INTEGER PRIMARY KEY, rating REAL, votes INTEGER) WITHOUT ROWID"
                    )
                    next(data)  # header: tconst averageRating numVotes
                    batch = []
                    for line in data:
                        tconst, rating, votes = line.decode().rstrip("\n").split("\t")
                        batch.append((int(tconst[2:]), float(rating), int(votes)))
                        if len(batch) >= 50_000:
                            db.executemany(
                                "INSERT INTO ratings VALUES (?, ?, ?)", batch
                            )
                            batch.clear()
                    db.executemany("INSERT INTO ratings VALUES (?, ?, ?)", batch)
                    db.commit()
            os.replace(tmp_path, self.path)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        self._count = 0
        logger.info(
            "IMDb ratings refreshed: %d titles in %.1f s",
            self.count,
            time.monotonic() - started,
        )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)

    def get(self, imdb_id: str | None) -> tuple[float, int] | None:
        """(rating, votes) for 'tt0111161', or None."""
        return self.many([imdb_id]).get(imdb_id) if imdb_id else None

    def many(self, imdb_ids: list[str | None]) -> dict[str, tuple[float, int]]:
        ids = {
            int(i[2:]): i
            for i in imdb_ids
            if i and i.startswith("tt") and i[2:].isdigit()
        }
        if not ids or not self.ready:
            return {}
        placeholders = ",".join("?" * len(ids))
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT id, rating, votes FROM ratings WHERE id IN ({placeholders})",
                list(ids),
            )
            return {ids[row[0]]: (row[1], row[2]) for row in rows}
