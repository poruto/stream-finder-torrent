"""Storage on the Status page (admin): what TorrServer keeps and what the cache takes.

TorrServer keeps every torrent ever played in its list (and database), and the app's
cache grows with subtitles and transcripts - on a Raspberry Pi's SD card that matters.
Lists them with sizes and removes them on request.

Also the backup next to it: the household's state and the server settings in one file,
and restoring such a file (onto a new SD card, another computer).
"""

import json
import logging
import os
import shutil
import time

from dotenv import dotenv_values
from flask import Blueprint, Response, jsonify, request

from . import config, notify, shared, version
from .api_helpers import json_body
from .auth import admin_required
from .i18n import t
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


# -- backup ---------------------------------------------------------------------------

BACKUP_FORMAT = 1


def _backup_keys(with_secrets: bool) -> list[str]:
    plain = [k for k in config.EDITABLE_KEYS if k not in config.SECRET_SETTINGS]
    return plain + list(config.SECRET_SETTINGS) if with_secrets else plain


def _saved_settings(with_secrets: bool) -> dict[str, str]:
    """What the Settings page saved into SETTINGS_FILE (only the known settings)."""
    if not os.path.exists(config.SETTINGS_FILE):
        return {}
    saved = dotenv_values(config.SETTINGS_FILE, interpolate=False)
    return {k: saved[k] for k in _backup_keys(with_secrets) if saved.get(k) is not None}


@library.route("/api/backup")
@admin_required
def backup():
    """One JSON file: the household's progress, lists and profiles (shared.json), the
    episodes already announced (notified.json) and the server settings; ?keys=1 adds
    the keys and passwords."""
    with_secrets = request.args.get("keys") == "1"
    with shared._lock:
        state = shared._read()
    data = {
        "streamfinder_backup": BACKUP_FORMAT,
        "app_version": version.VERSION,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "keys_included": with_secrets,
        "shared": state,
        "notified": notify._read_notified(),
        "settings": _saved_settings(with_secrets),
    }
    name = f"streamfinder-backup-{time.strftime('%Y-%m-%d')}.json"
    return Response(
        json.dumps(data, ensure_ascii=False, indent=1),
        mimetype="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            "Cache-Control": "no-store",
        },
    )


def _profile_meta(meta) -> dict | None:
    """A profile's name and colour from a backup, cleaned like /api/profiles does."""
    if not isinstance(meta, dict):
        return None
    try:
        color = max(0, min(11, int(meta.get("color") or 0)))
        created = float(meta.get("created") or time.time())
    except (TypeError, ValueError):
        color, created = 0, time.time()
    return {
        "name": str(meta.get("name") or "?").strip()[:30] or "?",
        "color": color,
        "created": created,
        **({"kids": True} if meta.get("kids") else {}),
    }


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _as_sent(state: dict, profile: str) -> dict:
    """One profile of a saved shared.json as a browser sends it to /api/shared."""
    source = _dict(state if not profile else _dict(state.get("data")).get(profile))
    items = source.get("list")
    return {
        "progress": _dict(source.get("progress")),
        "list": list(items.values()) if isinstance(items, dict) else items or [],
        "listRemoved": _dict(source.get("listRemoved")),
    }


def _restore_shared(incoming: dict) -> tuple[int, int]:
    """Merge every profile of a backup into shared.json; (profiles, titles) restored."""
    profiles = titles = 0
    backed_up = _dict(incoming.get("profiles"))
    with shared._lock:
        data = shared._read()
        named = dict(data.get("profiles") or {})
        for pid in ["", *backed_up]:
            if pid and not (isinstance(pid, str) and shared._PROFILE_ID.fullmatch(pid)):
                continue
            if pid and pid not in named:
                meta = _profile_meta(backed_up[pid])
                if meta is None or len(named) + 1 >= shared.MAX_PROFILES:
                    continue
                named[pid] = meta
            sent = _as_sent(incoming, pid)
            stored = shared.profile_part(data, pid)
            data = shared._store_part(data, pid, shared.merge(stored, sent))
            profiles += 1
            titles += len(sent["list"] if isinstance(sent["list"], list) else []) + len(
                [e for e in sent["progress"].values() if not _dict(e).get("removed")]
            )
        default = _profile_meta(incoming.get("defaultProfile"))
        if default and not data.get("defaultProfile"):
            data["defaultProfile"] = {
                "name": default["name"],
                "color": default["color"],
            }
        data["profiles"] = named
        shared._save(data)
    return profiles, titles


@library.route("/api/backup/restore", methods=["POST"])
@admin_required
def restore():
    """Body: a backup from /api/backup. Progress and lists are merged per profile like a
    device's copy (the newer entry wins), announced episodes are added, and the settings
    in the file replace the ones here."""
    body = json_body()
    shared_part, notified, settings = (
        body.get("shared"),
        body.get("notified") or {},
        body.get("settings") or {},
    )
    if (
        body.get("streamfinder_backup") != BACKUP_FORMAT
        or not isinstance(shared_part, dict)
        or not isinstance(notified, dict)
        or not isinstance(settings, dict)
    ):
        return jsonify(success=False, error=t("backup_invalid")), 400
    known = set(_backup_keys(True))
    changes = {
        k: v
        for k, v in settings.items()
        if k in known and isinstance(v, str) and not set(v) & {"\n", "\r", "\0"}
    }

    profiles, titles = _restore_shared(shared_part)
    current = notify._read_notified()
    announced = {
        str(k): v
        for k, v in notified.items()
        if isinstance(v, str) and str(k).isdigit()
    }
    if {**announced, **current} != current:
        shared.write_json(notify.STATE, {**announced, **current})
    if changes:
        config.save_settings(changes)
        svc = services()
        svc.apply_keys()
        if {"TORRSERVER_UPLOAD_LIMIT", "TORRSERVER_READ_AHEAD"} & changes.keys():
            svc.apply_network_later()
    logger.info(
        "Backup restored: %d profiles, %d titles, settings %s",
        profiles,
        titles,
        ", ".join(sorted(changes)) or "-",
    )
    return jsonify(
        success=True, profiles=profiles, titles=titles, settings=sorted(changes)
    )
