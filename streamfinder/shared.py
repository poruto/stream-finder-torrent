"""Watch progress and "My list" shared by every device of the household.

Each browser keeps its own copy (localStorage) and merges it with this one, so a film
started on the TV continues on the phone. Entry by entry the newer change wins;
removals stay as markers ({"removed": true} / listRemoved) so that another device
can't bring a removed entry back. Markers are dropped after 90 days.
"""

import json
import logging
import os
import tempfile
import threading
import time

from flask import Blueprint, jsonify, request

from . import config

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
