"""Phone notifications when a new episode of a followed show can actually be played.

Every few hours the household's shows (shared progress and My list, see shared.py) are
checked: an episode that aired in the last two weeks, comes after the last one watched
and already has a version with seeders is announced once - through ntfy
(NOTIFY_NTFY_URL, e.g. https://ntfy.sh/<own topic>, the ntfy app on the phone) or a
Telegram bot (NOTIFY_TELEGRAM_TOKEN + NOTIFY_TELEGRAM_CHAT). With neither set it sleeps.
"""

import datetime
import json
import logging
import os
import socket
import tempfile
import threading
import time

import requests
from flask import Blueprint, jsonify, request

from . import config, shared
from .auth import admin_required
from .i18n import t
from .services import services
from .tmdb import TMDBError

logger = logging.getLogger(__name__)

notify = Blueprint("notify", __name__)

CHECK_EVERY = 3 * 3600
FIRST_CHECK = 120  # seconds after start: TorrServer and the indexes have woken up
RECENT_DAYS = 14
STATE = os.path.join(config.BASE_DIR, "cache", "notified.json")
_status = {"checked": None, "sent": []}  # shown in Settings


def _setting(name: str) -> str:
    return (getattr(config, name, "") or os.getenv(name, "")).strip()


def configured() -> bool:
    return bool(
        _setting("NOTIFY_NTFY_URL")
        or (_setting("NOTIFY_TELEGRAM_TOKEN") and _setting("NOTIFY_TELEGRAM_CHAT"))
    )


def episode_number(season: int, episode: int) -> int:
    return season * 10000 + episode


def followed_shows(state: dict) -> dict[int, int]:
    """Show id -> number of the furthest episode watched (0 = only in My list)."""
    shows: dict[int, int] = {}
    for key, entry in (state.get("progress") or {}).items():
        if not key.startswith("tv:") or entry.get("removed") or not key[3:].isdigit():
            continue
        seen = [k.split("x") for k in (entry.get("episodes") or {})]
        numbers = [
            episode_number(int(s), int(e))
            for s, e in seen
            if s.isdigit() and e.isdigit()
        ]
        if entry.get("season") and entry.get("episode"):
            numbers.append(episode_number(int(entry["season"]), int(entry["episode"])))
        shows[int(key[3:])] = max(numbers, default=0)
    for item in (state.get("list") or {}).values():
        if item.get("type") == "tv" and str(item.get("id", "")).isdigit():
            shows.setdefault(int(item["id"]), 0)
    return shows


def due(latest: dict | None, watched: int, today: datetime.date) -> bool:
    """A recently aired episode after the last one watched."""
    if not latest or not latest.get("air_date"):
        return False
    try:
        aired = datetime.date.fromisoformat(latest["air_date"])
    except ValueError:
        return False
    recent = today - datetime.timedelta(days=RECENT_DAYS) <= aired <= today
    return recent and episode_number(latest["season"], latest["episode"]) > watched


def playable(tmdb_id: int, season: int, episode: int) -> dict | None:
    """The version the player would pick for that episode, when it has seeders."""
    from .api import search_query  # the same search as the player's

    query = search_query(
        {"tmdb_id": tmdb_id, "media_type": "tv", "season": season, "episode": episode}
    )
    result = services().torrents.search(query) if query else {}
    pick = (result.get("picks") or {}).get("recommended")
    best = next((r for r in result.get("results") or [] if r.get("hash") == pick), None)
    return best if best and (best.get("seeders") or 0) > 0 else None


def _lan_url() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(
                ("192.0.2.1", 9)
            )  # no packet is sent; just picks the network address
            host = s.getsockname()[0]
        except OSError:
            host = "127.0.0.1"
    return f"http://{host}:{config.PORT}"


def send(title: str, text: str, link: str = "") -> None:
    """Through every configured channel; raises requests.RequestException on failure."""
    if url := _setting("NOTIFY_NTFY_URL"):
        # Title and link as URL parameters: headers can't carry Czech letters.
        params = {"title": title, "tags": "tv", **({"click": link} if link else {})}
        requests.post(
            url, params=params, data=text.encode("utf-8"), timeout=15
        ).raise_for_status()
    token, chat = _setting("NOTIFY_TELEGRAM_TOKEN"), _setting("NOTIFY_TELEGRAM_CHAT")
    if token and chat:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat,
                "text": "\n".join(filter(None, [title, text, link])),
            },
            timeout=15,
        ).raise_for_status()


def _read_notified() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_notified(data: dict) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STATE), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, STATE)


def check_once(today: datetime.date | None = None) -> list[str]:
    """Announce what became playable; returns the messages sent."""
    today = today or datetime.date.today()
    tmdb = services().tmdb
    notified = _read_notified()
    sent = []
    base = _setting("PUBLIC_URL").rstrip("/") or _lan_url()
    shows: dict[int, int] = {}
    for part in shared.all_parts(
        shared._read()
    ):  # new until everyone who follows it saw it
        for show_id, watched in followed_shows(part).items():
            shows[show_id] = min(shows.get(show_id, watched), watched)
    for tmdb_id, watched in shows.items():
        try:
            latest = tmdb.latest_episode(tmdb_id)
        except TMDBError:
            continue
        if not due(latest, watched, today):
            continue
        tag = f"S{latest['season']:02d}E{latest['episode']:02d}"
        if notified.get(str(tmdb_id)) == tag:
            continue
        version = playable(tmdb_id, latest["season"], latest["episode"])
        if not version:
            continue  # aired, but nothing to play yet: asked again next time
        name = tmdb.details("tv", tmdb_id).get("name") or ""
        title = t("notify_title", show=name)
        text = t(
            "notify_text",
            episode=tag,
            quality=version.get("quality") or "",
            seeders=version.get("seeders") or 0,
        )
        link = (
            f"{base}/show/{tmdb_id}?play=1&s={latest['season']}&e={latest['episode']}"
        )
        try:
            send(title, text, link)
        except requests.RequestException as e:
            logger.warning("notification failed: %s", e)
            break
        notified[str(tmdb_id)] = tag
        sent.append(f"{name} {tag}")
    if sent:
        _write_notified(notified)
    _status.update(checked=time.time(), sent=sent or _status["sent"])
    return sent


def start(app) -> None:
    """The background check (one thread; the app runs in one process)."""

    def loop():
        time.sleep(FIRST_CHECK)
        while True:
            if configured():
                try:
                    with app.app_context():  # services() lives on the app
                        check_once()
                except (
                    Exception
                ):  # never let the thread die; logged and tried again later
                    logger.exception("new-episode check failed")
            time.sleep(CHECK_EVERY)

    threading.Thread(target=loop, name="notify", daemon=True).start()


@notify.route("/api/notify")
@admin_required
def notify_status():
    return jsonify(
        success=True,
        ntfy=_setting("NOTIFY_NTFY_URL"),
        telegram=bool(
            _setting("NOTIFY_TELEGRAM_TOKEN") and _setting("NOTIFY_TELEGRAM_CHAT")
        ),
        checked=_status["checked"],
        sent=_status["sent"],
    )


@notify.route("/api/notify", methods=["POST"])
@admin_required
def notify_update():
    """Body: {ntfy_url?, telegram_token?, telegram_chat?, remove?: ["ntfy", "telegram"],
    test?: true} - saves the channels, then (test) sends a test message through them."""
    body = request.get_json(silent=True) or {}
    value = lambda name: str(body.get(name) or "").strip()
    changes = {}
    if value("ntfy_url"):
        if not value("ntfy_url").startswith(("http://", "https://")):
            return jsonify(success=False, error=t("url_invalid")), 400
        changes["NOTIFY_NTFY_URL"] = value("ntfy_url")
    if value("telegram_token") and value("telegram_chat"):
        changes.update(
            NOTIFY_TELEGRAM_TOKEN=value("telegram_token"),
            NOTIFY_TELEGRAM_CHAT=value("telegram_chat"),
        )
    for channel in body.get("remove") or []:
        if channel == "ntfy":
            changes["NOTIFY_NTFY_URL"] = ""
        elif channel == "telegram":
            changes.update(NOTIFY_TELEGRAM_TOKEN="", NOTIFY_TELEGRAM_CHAT="")
    if changes:
        config.save_settings(changes)
    if body.get("test"):
        if not configured():
            return jsonify(success=False, error=t("notify_none")), 400
        try:
            send(
                t("notify_test_title"),
                t("notify_test_text"),
                _setting("PUBLIC_URL") or _lan_url(),
            )
        except requests.RequestException as e:
            return jsonify(success=False, error=t("notify_failed", error=str(e))), 502
    return notify_status()
