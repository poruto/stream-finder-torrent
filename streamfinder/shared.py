"""Watch progress and "My list" shared by every device of the household.

Each browser keeps its own copy (localStorage) and merges it with this one, so a film
started on the TV continues on the phone. Entry by entry the newer change wins;
removals stay as markers ({"removed": true} / listRemoved) so that another device
can't bring a removed entry back. Markers are dropped after 90 days.

Also the small TMDB lookups the home page builds on that state: new and next episodes
of the shows being watched, and recommendations for the titles watched last.
"""

import json
import logging
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Blueprint, jsonify, request

from . import config
from .i18n import tmdb_locale
from .services import services
from .tmdb import TMDBError
from .views import cards

logger = logging.getLogger(__name__)

shared = Blueprint("shared", __name__)
PATH = os.path.join(config.BASE_DIR, "cache", "shared.json")
KEEP_MARKERS = 90 * 86400 * 1000  # ms, like the browsers' Date.now()
_lock = threading.Lock()


def _empty() -> dict:
    return {"progress": {}, "list": {}, "listRemoved": {}}


def _time(value, field: str) -> float:
    try:
        return float((value or {}).get(field) or 0)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def merge(stored: dict, incoming: dict, now_ms: float | None = None) -> dict:
    """`incoming` (one browser's copy) merged into `stored`; the newer entry wins."""
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    result = {key: dict(stored.get(key) or {}) for key in _empty()}

    for key, entry in (incoming.get("progress") or {}).items():
        if isinstance(entry, dict) and _time(entry, "updated") > _time(
            result["progress"].get(key), "updated"
        ):
            result["progress"][key] = entry

    for item in incoming.get("list") or []:
        key = isinstance(item, dict) and item.get("key")
        if key and _time(item, "added") > _time(result["list"].get(key), "added"):
            result["list"][key] = item
    for key, when in (incoming.get("listRemoved") or {}).items():
        if isinstance(when, (int, float)) and when > result["listRemoved"].get(key, 0):
            result["listRemoved"][key] = when
    # An entry added after its removal is back; one removed after it was added is gone.
    for key, when in list(result["listRemoved"].items()):
        if key in result["list"] and when >= _time(result["list"][key], "added"):
            del result["list"][key]

    old = now_ms - KEEP_MARKERS
    result["progress"] = {
        k: v
        for k, v in result["progress"].items()
        if not (v.get("removed") and _time(v, "updated") < old)
    }
    result["listRemoved"] = {k: v for k, v in result["listRemoved"].items() if v >= old}
    return result


def _read() -> dict:
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else _empty()
    except FileNotFoundError:
        return _empty()
    except (OSError, ValueError) as e:
        logger.warning("shared state unreadable, starting over: %s", e)
        return _empty()


def _write(data: dict) -> None:
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(PATH), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, PATH)
    except OSError:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _response(data: dict):
    items = sorted(data["list"].values(), key=lambda i: _time(i, "added"), reverse=True)
    return jsonify(
        success=True,
        progress=data["progress"],
        list=items,
        listRemoved=data["listRemoved"],
    )


@shared.route("/api/shared", methods=["GET", "POST"])
def shared_state():
    """GET: the household's copy. POST {progress, list, listRemoved}: merge and return it."""
    with _lock:
        data = _read()
        if request.method == "POST":
            incoming = request.get_json(silent=True, force=True) or {}
            merged = merge(data, incoming)
            if merged != data:
                try:
                    _write(merged)
                except OSError as e:  # read-only disk: still answer with the merge
                    logger.warning("shared state not saved: %s", e)
            data = merged
    return _response(data)


@shared.route("/api/new-episodes")
def new_episodes():
    """?ids=1,2,3 (TMDB show ids): each show's last aired episode, for "New episode" badges."""
    ids = [int(i) for i in request.args.get("ids", "").split(",") if i.isdigit()][:40]
    tmdb = services().tmdb

    def latest(tmdb_id: int):
        try:
            return tmdb_id, tmdb.latest_episode(tmdb_id)
        except TMDBError:
            return tmdb_id, None

    with ThreadPoolExecutor(max_workers=6) as pool:
        found = dict(pool.map(latest, ids))
    return jsonify(success=True, latest={str(k): v for k, v in found.items() if v})


def _numbers(episode: dict | None) -> tuple[int, int] | None:
    episode = episode or {}
    if not episode.get("season_number") or not episode.get("episode_number"):
        return None
    return episode["season_number"], episode["episode_number"]


def next_episode(
    tmdb, tmdb_id: int, season: int, episode: int, today: str | None = None
):
    """The episode after S{season}E{episode}: {season, episode, aired, air_date}, or None.

    The last episode of a season rolls over to the first of the next one; specials
    (season 0) are never "next". The cached /tv/{id} summary (shared with "New episode")
    answers most cases; a season's episode list is only fetched for the air date of an
    episode that hasn't aired yet and isn't the one TMDB announces as next.
    """
    if season < 1 or episode < 1:
        return None
    today = today or time.strftime("%Y-%m-%d")
    show = tmdb.show_summary(tmdb_id)
    seasons = {
        s["season_number"]: s
        for s in show.get("seasons") or []
        if (s.get("season_number") or 0) > 0
    }
    announced = _numbers(show.get("next_episode_to_air"))
    if episode < (seasons.get(season, {}).get("episode_count") or 0):
        wanted = (season, episode + 1)
    elif later := sorted(n for n in seasons if n > season):
        wanted = (later[0], 1)
    elif announced and announced > (season, episode):
        wanted = announced  # the summary's episode counts lag behind
    else:
        return None  # the last episode there is

    last = _numbers(show.get("last_episode_to_air"))
    air_date = None
    if not last or wanted > last:
        if wanted == announced:
            air_date = show["next_episode_to_air"].get("air_date")
        elif wanted[1] == 1 and seasons.get(wanted[0], {}).get("air_date"):
            air_date = seasons[wanted[0]]["air_date"]
        else:
            episodes = tmdb.season_summary(tmdb_id, wanted[0]).get("episodes") or []
            air_date = next(
                (
                    e.get("air_date")
                    for e in episodes
                    if e.get("episode_number") == wanted[1]
                ),
                None,
            )
    aired = bool(last and wanted <= last) or bool(air_date and air_date <= today)
    return {
        "season": wanted[0],
        "episode": wanted[1],
        "aired": aired,
        "air_date": air_date,
    }


@shared.route("/api/next-episode")
def next_episodes():
    """?ids=1:2:5,... (show id:season:episode last watched): each show's next episode."""
    wanted = []
    for part in request.args.get("ids", "").split(",")[:40]:
        key = part.removeprefix("tv:")
        numbers = key.split(":")
        if len(numbers) == 3 and all(n.isdigit() for n in numbers):
            wanted.append((key, *map(int, numbers)))
    tmdb = services().tmdb

    def one(item):
        key, tmdb_id, season, episode = item
        try:
            return key, next_episode(tmdb, tmdb_id, season, episode)
        except TMDBError:
            return key, None

    with ThreadPoolExecutor(max_workers=6) as pool:
        found = dict(pool.map(one, wanted))
    return jsonify(success=True, next={k: v for k, v in found.items() if v})


@shared.route("/api/because")
def because():
    """?keys=movie:1,tv:2 (up to 3 titles): TMDB's recommendations for each, as poster cards."""
    keys = []
    for key in request.args.get("keys", "").split(","):
        kind, _, tmdb_id = key.partition(":")
        if kind in ("movie", "tv") and tmdb_id.isdigit() and key not in keys:
            keys.append(key)
    keys = keys[:3]
    tmdb = services().tmdb
    language = tmdb_locale()  # the worker threads have no request to read it from

    def one(key):
        kind, _, tmdb_id = key.partition(":")
        try:
            return key, tmdb.recommendations(kind, int(tmdb_id), language)
        except TMDBError:
            return key, []

    with ThreadPoolExecutor(max_workers=3) as pool:
        found = list(pool.map(one, keys))
    rows = [
        {"key": key, "items": cards(results, key.partition(":")[0])}
        for key, results in found
    ]
    return jsonify(success=True, rows=[r for r in rows if r["items"]])
