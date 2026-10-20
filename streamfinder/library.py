"""Storage on the Status page (admin): what TorrServer keeps and what the cache takes.

TorrServer keeps every torrent ever played in its list (and database), and the app's
cache grows with subtitles and transcripts - on a Raspberry Pi's SD card that matters.
Lists them with sizes and removes them on request.
"""

import logging
import os
import shutil

from flask import Blueprint, jsonify, request

from . import config
from .auth import admin_required
from .services import services
from .torrserver import TorrServerError

logger = logging.getLogger(__name__)

library = Blueprint("library", __name__)


def folder_size(path: str) -> int:
    """Bytes in `path` (a file or a folder tree); 0 when it doesn't exist."""
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass  # removed meanwhile
    return total


def _whisper_models() -> str:
    return os.path.join(
        os.getenv("WHISPER_DIR") or os.path.expanduser("~/whisper.cpp"), "models"
    )


def _torrents() -> list[dict]:
    items = services().torrserver._post({"action": "list"}) or []
    return sorted(
        (
            {
                "hash": t.get("hash"),
                "title": t.get("title") or t.get("name") or "",
                "poster": t.get("poster") or "",
                "size": t.get("torrent_size") or 0,
                "added": t.get("timestamp") or 0,
                "active": t.get("stat")
                in (1, 2, 3),  # getting info / preloading / working
            }
            for t in items
            if t.get("hash")
        ),
        key=lambda t: t["added"],
        reverse=True,
    )


@library.route("/api/library")
@admin_required
def overview():
    cache = os.path.join(config.BASE_DIR, "cache")
    try:
        torrents, error = _torrents(), None
    except TorrServerError as e:
        torrents, error = [], str(e)
    return jsonify(
        success=True,
        torrents=torrents,
        torrserver_error=error,
        disk={
            "subtitles": folder_size(config.SUBTITLE_CACHE_DIR),
            "imdb": folder_size(config.IMDB_DB_PATH),
            "cache": folder_size(cache),
            "whisper": folder_size(_whisper_models()),
        },
        free=shutil.disk_usage(cache if os.path.isdir(cache) else config.BASE_DIR).free,
    )


@library.route("/api/library/remove", methods=["POST"])
@admin_required
def remove():
    """Body: {hash} or {all: true} - takes torrents off TorrServer's list (and its cache)."""
    body = request.get_json(silent=True) or {}
    try:
        hashes = (
            [t["hash"] for t in _torrents()] if body.get("all") else [body.get("hash")]
        )
        for info_hash in filter(None, hashes):
            services().torrserver._post({"action": "rem", "hash": info_hash})
    except TorrServerError as e:
        return jsonify(success=False, error=str(e)), 502
    return jsonify(success=True, removed=len([h for h in hashes if h]))


@library.route("/api/library/clear-subtitles", methods=["POST"])
@admin_required
def clear_subtitles():
    """Downloaded and translated subtitles; they are fetched again when needed."""
    folder = config.SUBTITLE_CACHE_DIR
    freed = folder_size(folder)
    for name in os.listdir(folder) if os.path.isdir(folder) else []:
        path = os.path.join(folder, name)
        try:
            shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
        except OSError as e:
            logger.warning("can't remove %s: %s", path, e)
    return jsonify(success=True, freed=freed - folder_size(folder))
