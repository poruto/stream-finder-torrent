"""Small helpers shared by the JSON API's blueprints."""

import json
import logging
import re
from typing import Any

from flask import Response, abort, jsonify, request

from .i18n import t
from .services import services
from .torrserver import TorrServerError

logger = logging.getLogger(__name__)

HASH_RE = re.compile(r"[a-f0-9]{40}")


def json_body() -> dict[str, Any]:
    """The request's JSON object, or a 400 answer."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(
            Response(
                json.dumps({"success": False, "error": t("err_json")}),
                400,
                mimetype="application/json",
            )
        )
    return data


def as_int(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def as_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_media_type(value) -> str:
    return value if value in ("movie", "tv") else "movie"


def id_list(name: str, limit: int = 40) -> list[int]:
    """?<name>=1,2,3: the TMDB ids in a query argument (at most `limit`)."""
    return [int(i) for i in request.args.get(name, "").split(",") if i.isdigit()][
        :limit
    ]


def torrserver_error(error: TorrServerError):
    logger.warning("TorrServer: %s", error)
    return jsonify(success=False, error=t(f"ts_{error.code}")), 502


def torrent_file(info_hash: str, index: int):
    """The TorrentFile for a stream, or 404."""
    if not HASH_RE.fullmatch(info_hash):
        abort(404)
    try:
        file = services().torrserver.file(info_hash, index, wait=10)
    except TorrServerError:
        abort(502)
    if file is None or not file.is_video:
        abort(404)
    return file
