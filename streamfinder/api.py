"""JSON API used by the frontend: the catalogue (browse, search, seasons) and health.

The rest lives by area: playback.py, subtitle_api.py, admin.py (server settings),
shared.py, library.py, notify.py and upcoming.py.
"""

from flask import Blueprint, jsonify, request

from .api_helpers import as_int, parse_media_type
from .playback import search_query  # noqa: F401 - the notifier's and older imports
from .services import services
from .views import browse_filters, browse_page, search_items, season_episodes

api = Blueprint("api", __name__, url_prefix="/api")


@api.route("/browse")
def browse():
    page = max(1, min(as_int(request.args.get("page")) or 1, 500))
    return jsonify(
        browse_page(
            parse_media_type(request.args.get("type")),
            browse_filters(request.args),
            page,
        )
    )


@api.route("/search")
def search():
    query = request.args.get("q", "").strip()
    kind = (
        request.args.get("type")
        if request.args.get("type") in ("movie", "tv")
        else None
    )
    return jsonify(items=search_items(query, kind) if query else [])


@api.route("/season/<int:tmdb_id>/<int:season_number>")
def season(tmdb_id: int, season_number: int):
    return jsonify(episodes=season_episodes(tmdb_id, season_number))


@api.route("/torrserver-status")
def torrserver_status():
    return jsonify(status="online" if services().torrserver.is_online() else "offline")


@api.route("/healthz")
def healthz():
    return "ok"
