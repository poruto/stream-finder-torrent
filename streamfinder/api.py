"""JSON API used by the frontend."""

import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

from flask import Blueprint, Response, abort, jsonify, request

from . import config
from .i18n import t
from .services import services
from .tmdb import match_titles
from .torrents import SearchQuery
from .torrserver import (
    TorrServerError,
    normalize_magnet,
    select_video_file,
    video_files,
)
from .views import (
    browse_filters,
    browse_page,
    search_items,
    season_episodes,
    year_of,
)

logger = logging.getLogger(__name__)
api = Blueprint("api", __name__, url_prefix="/api")

_HASH_RE = re.compile(r"[a-f0-9]{40}")


def _body() -> dict[str, Any]:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(
            Response(
                '{"success": false, "error": "%s"}' % t("err_json"),
                400,
                mimetype="application/json",
            )
        )
    return data


def _int(value) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _media_type(value) -> str:
    return value if value in ("movie", "tv") else "movie"


def _torrserver_error(error: TorrServerError):
    logger.warning("TorrServer: %s", error)
    return jsonify(success=False, error=t(f"ts_{error.code}")), 502


def _public_torrserver_url() -> str:
    """TorrServer URL as reachable from the user's browser.

    Inside Docker the app talks to http://127.0.0.1:8090 (host network) and the
    browser may be another device, so by default reuse the host the browser used
    to open this app.
    """
    if config.TORRSERVER_PUBLIC_URL:
        return config.TORRSERVER_PUBLIC_URL
    parsed = urlparse(config.TORRSERVER_URL)
    host = (
        request.host if request.host.endswith("]") else request.host.rsplit(":", 1)[0]
    )
    return f"{parsed.scheme}://{host}:{parsed.port or 8090}"


@api.route("/browse")
def browse():
    page = max(1, min(_int(request.args.get("page")) or 1, 500))
    return jsonify(browse_page(_media_type(request.args.get("type")), browse_filters(request.args), page))


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


@api.route("/torrents", methods=["POST"])
def torrents():
    """Body: {tmdb_id, media_type, season?, episode?, runtime?, quality?, query?}; `query` = free text."""
    body = _body()
    media_type = _media_type(body.get("media_type"))
    tmdb_id = _int(body.get("tmdb_id"))
    free_text = (body.get("query") or "").strip()
    season, episode = _int(body.get("season")), _int(body.get("episode"))
    runtime = _int(body.get("runtime"))
    quality = _int(body.get("quality"))
    quality = quality if quality in (2160, 1080, 720) else None

    data = services().tmdb.details(media_type, tmdb_id) if tmdb_id else {}
    # Episode counts let packs be judged by size per episode, not total size.
    season_counts = {
        s["season_number"]: s.get("episode_count") or 0
        for s in data.get("seasons") or []
        if s.get("season_number") is not None
    }
    common = dict(
        media_type=media_type,
        season=season,
        episode=episode,
        quality=quality,
        season_episodes=season_counts,
        total_episodes=data.get("number_of_episodes"),
        runtime=runtime
        or data.get("runtime")
        or (data.get("episode_run_time") or [None])[0],
    )
    if free_text:
        query = SearchQuery(
            title=free_text, titles=[free_text], free_text=True, **common
        )
    elif tmdb_id:
        titles = match_titles(data)
        query = SearchQuery(
            title=data.get("english_title") or titles[0],
            titles=titles,
            year=year_of(data),
            imdb_id=data.get("imdb_id"),
            **common,
        )
    else:
        return jsonify(success=False, error=t("err_missing_query")), 400
    return jsonify(success=True, **services().torrents.search(query))


@api.route("/play", methods=["POST"])
def play():
    """Add a torrent to TorrServer and pick the file to stream.

    Body: {magnet, season?, episode?, file_index?, title?, poster?}
    """
    body = _body()
    magnet = normalize_magnet(body.get("magnet", ""))
    if not magnet:
        return jsonify(success=False, error=t("err_bad_magnet")), 400
    torrserver = services().torrserver
    try:
        info_hash = torrserver.add(
            magnet, title=body.get("title") or "", poster=body.get("poster") or ""
        )
        files = torrserver.files(info_hash, wait=45)
    except TorrServerError as e:
        return _torrserver_error(e)
    if not files:
        return jsonify(
            success=False, retry=True, hash=info_hash, error=t("err_no_seeds")
        ), 504

    season, episode = _int(body.get("season")), _int(body.get("episode"))
    file_index = _int(body.get("file_index"))

    def choose(files):
        return next(
            (f for f in files if f.index == file_index and f.is_video), None
        ) or select_video_file(files, season, episode)

    chosen = choose(files)
    # Right after metadata arrives TorrServer sometimes reports an incomplete file list.
    for _ in range(3):
        if chosen is not None:
            break
        time.sleep(2)
        try:
            files = torrserver.files(info_hash, wait=5)
        except TorrServerError as e:
            return _torrserver_error(e)
        chosen = choose(files)
    if chosen is None:
        logger.warning("No video in %s: %s", info_hash, [f.path for f in files])
        return jsonify(success=False, error=t("err_no_video")), 422
    return jsonify(
        success=True,
        hash=info_hash,
        magnet=magnet,
        stream_url=torrserver.stream_url(info_hash, chosen, _public_torrserver_url()),
        file=chosen.to_dict(),
        files=[f.to_dict() for f in video_files(files)],
    )


@api.route("/torrent-stats/<info_hash>")
def torrent_stats(info_hash: str):
    if not _HASH_RE.fullmatch(info_hash):
        abort(404)
    try:
        status = services().torrserver.get(info_hash) or {}
    except TorrServerError as e:
        return _torrserver_error(e)
    return jsonify(
        success=True,
        seeders=status.get("connected_seeders", 0),
        peers=status.get("active_peers", 0),
        download_speed=int(status.get("download_speed") or 0),
    )


@api.route("/torrserver-status")
def torrserver_status():
    return jsonify(status="online" if services().torrserver.is_online() else "offline")


@api.route("/subtitles/find", methods=["POST"])
def find_subtitles():
    """Body: {hash, file_index, tmdb_id, media_type, season?, episode?, languages?}"""
    body = _body()
    info_hash = (body.get("hash") or "").lower()
    if not _HASH_RE.fullmatch(info_hash):
        return jsonify(success=False, error=t("err_missing_hash")), 400
    media_type = _media_type(body.get("media_type"))
    tmdb_id = _int(body.get("tmdb_id"))
    season, episode = _int(body.get("season")), _int(body.get("episode"))
    svc = services()
    data = svc.tmdb.details(media_type, tmdb_id) if tmdb_id else {}
    episode_imdb = None
    if media_type == "tv" and tmdb_id and season is not None and episode is not None:
        episode_imdb = svc.tmdb.episode_imdb_id(tmdb_id, season, episode)
    languages = [
        lang
        for lang in body.get("languages") or []
        if isinstance(lang, str) and len(lang) == 2
    ] or None
    try:
        result = svc.subtitles.find(
            info_hash,
            _int(body.get("file_index")),
            tmdb_id=tmdb_id,
            media_type=media_type,
            season=season,
            episode=episode,
            imdb_id=data.get("imdb_id"),
            episode_imdb_id=episode_imdb,
            title=data.get("english_title") or "",
            year=year_of(data),
            languages=languages,
        )
    except TorrServerError as e:
        return _torrserver_error(e)
    return jsonify(success=True, **result)


@api.route("/subtitles/<candidate_id>.vtt")
def subtitle_vtt(candidate_id: str):
    try:
        vtt = services().subtitles.get_vtt(candidate_id)
    except ValueError as e:
        return Response(str(e), 404, mimetype="text/plain")
    except Exception as e:  # network / quota errors from providers
        logger.warning("Subtitle %s failed: %s", candidate_id, e)
        return Response(str(e), 502, mimetype="text/plain")
    return Response(
        vtt, mimetype="text/vtt", headers={"Cache-Control": "private, max-age=86400"}
    )


@api.route("/healthz")
def healthz():
    return "ok"
