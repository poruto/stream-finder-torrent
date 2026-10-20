"""JSON API used by the frontend."""

import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

import requests
from flask import Blueprint, Response, abort, jsonify, request, stream_with_context

from . import auth, config
from .auth import admin_required
from .i18n import t
from .services import services
from .subtitles.sync import align
from .tmdb import TMDBError, match_titles
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
                json.dumps({"success": False, "error": t("err_json")}),
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
    return jsonify(
        browse_page(
            _media_type(request.args.get("type")), browse_filters(request.args), page
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


def _torrent_file(info_hash: str, index: int):
    """The TorrentFile for a stream, or 404."""
    if not _HASH_RE.fullmatch(info_hash):
        abort(404)
    try:
        files = services().torrserver.files(info_hash, wait=10)
    except TorrServerError:
        abort(502)
    file = next((f for f in files if f.index == index and f.is_video), None)
    if file is None:
        abort(404)
    return file


@api.route("/probe/<info_hash>/<int:index>")
def probe(info_hash: str, index: int):
    """Codecs of a playing file, so the player knows whether audio needs converting."""
    svc = services()
    if not svc.transcoder.available:
        return jsonify(success=True, available=False)
    file = _torrent_file(info_hash, index)
    try:
        result = svc.transcoder.probe(svc.torrserver.stream_url(info_hash, file))
    except Exception as e:  # ffprobe timeout / unreadable stream
        logger.info("probe failed for %s: %s", file.name, e)
        return jsonify(success=True, available=True, probed=False)
    return jsonify(success=True, available=True, probed=True, **result.to_dict())


@api.route("/keyframe/<info_hash>/<int:index>")
def keyframe(info_hash: str, index: int):
    """Exact start time of a converted stream that is asked to begin at ?t=."""
    svc = services()
    if not svc.transcoder.available:
        abort(404)
    file = _torrent_file(info_hash, index)
    position = max(0.0, request.args.get("t", 0, type=float))
    try:
        start = svc.transcoder.keyframe_at(
            svc.torrserver.stream_url(info_hash, file), position
        )
    except Exception as e:  # timeout: fall back to the requested time
        logger.info("keyframe lookup failed: %s", e)
        start = position
    return jsonify(success=True, start=start)


@api.route("/transcode/<info_hash>/<int:index>.mp4")
def transcode(info_hash: str, index: int):
    """Video copied as-is, audio converted to AAC, streamed as fragmented MP4.

    Query: start (seconds), audio (track number), copy=1 to only remux (Matroska -> MP4).
    """
    svc = services()
    if not svc.transcoder.available:
        abort(404)
    file = _torrent_file(info_hash, index)
    url = svc.torrserver.stream_url(info_hash, file)
    start = max(0.0, request.args.get("start", 0, type=float))
    track = max(0, request.args.get("audio", 0, type=int))
    try:
        video_codec = svc.transcoder.probe(url).video_codec
    except Exception:
        video_codec = None
    chunks = svc.transcoder.stream(
        url,
        start,
        track,
        copy_audio=request.args.get("copy") == "1",
        video_codec=video_codec,
    )
    return Response(
        stream_with_context(chunks),
        mimetype="video/mp4",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
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


@api.route("/subtitles/sync", methods=["POST"])
def sync_subtitles():
    """Align a subtitle to the speech around `position`.

    Body: {hash, file_index, subtitle (candidate id), position}
    Returns {offset, scale, confidence, applied}: cue times become t * scale + offset.
    """
    body = _body()
    svc = services()
    if not svc.transcoder.available:
        return jsonify(success=False, error="ffmpeg is not installed"), 501
    file = _torrent_file(
        (body.get("hash") or "").lower(), _int(body.get("file_index")) or 0
    )
    position = max(0.0, float(body.get("position") or 0))
    try:
        cues = svc.subtitles.cues(str(body.get("subtitle") or ""))
        window_start = max(0.0, position - 30)
        speech = svc.transcoder.speech(
            svc.torrserver.stream_url((body.get("hash") or "").lower(), file),
            window_start,
        )
    except ValueError as e:
        return jsonify(success=False, error=str(e)), 404
    except Exception as e:  # ffmpeg timeout, download failure
        logger.info("subtitle sync failed: %s", e)
        return jsonify(success=False, error=str(e)), 502
    if not speech:
        return jsonify(success=False, error="no speech found"), 422
    result = align(cues, speech, (window_start, window_start + 120))
    return jsonify(success=True, **result.to_dict())


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


def _key_status() -> dict:
    return {
        "tmdb": bool(config.TMDB_API_KEY),
        "opensubtitles": bool(config.OPENSUBTITLES_API_KEY),
        "opensubtitles_login": bool(
            config.OPENSUBTITLES_USERNAME and config.OPENSUBTITLES_PASSWORD
        ),
    }


@api.route("/keys")
@admin_required
def keys_status():
    """Which keys are configured. The keys themselves are never sent back."""
    return jsonify(success=True, **_key_status())


@api.route("/keys", methods=["POST"])
@admin_required
def keys_update():
    """Body: any of {tmdb_api_key, opensubtitles_api_key, opensubtitles_username, opensubtitles_password}
    plus optional remove: ["opensubtitles"]. Every new key is tested before it is saved."""
    body = _body()
    svc = services()
    value = lambda name: (
        (body.get(name) or "").strip() if isinstance(body.get(name), str) else ""
    )  # noqa: E731
    changes: dict[str, str] = {}

    tmdb_key = value("tmdb_api_key")
    if tmdb_key:
        try:
            if not svc.tmdb.check_key(tmdb_key):
                return jsonify(
                    success=False, field="tmdb", error=t("key_invalid", name="TMDB")
                ), 400
        except TMDBError:
            return jsonify(
                success=False, field="tmdb", error=t("key_unreachable", name="TMDB")
            ), 502
        changes["TMDB_API_KEY"] = tmdb_key

    os_key = value("opensubtitles_api_key") or config.OPENSUBTITLES_API_KEY
    if value("opensubtitles_api_key"):
        try:
            if not svc.subtitles.api.check_key(os_key):
                return jsonify(
                    success=False,
                    field="opensubtitles",
                    error=t("key_invalid", name="OpenSubtitles"),
                ), 400
        except requests.RequestException:
            return jsonify(
                success=False,
                field="opensubtitles",
                error=t("key_unreachable", name="OpenSubtitles"),
            ), 502
        changes["OPENSUBTITLES_API_KEY"] = os_key

    username, password = (
        value("opensubtitles_username"),
        value("opensubtitles_password"),
    )
    if username and password:
        if not os_key:
            return jsonify(success=False, field="login", error=t("key_needs_api")), 400
        try:
            if not svc.subtitles.api.check_login(os_key, username, password):
                return jsonify(
                    success=False, field="login", error=t("login_invalid")
                ), 400
        except requests.RequestException:
            return jsonify(
                success=False,
                field="login",
                error=t("key_unreachable", name="OpenSubtitles"),
            ), 502
        changes.update(OPENSUBTITLES_USERNAME=username, OPENSUBTITLES_PASSWORD=password)

    if "opensubtitles" in (body.get("remove") or []):
        changes.update(
            OPENSUBTITLES_API_KEY="",
            OPENSUBTITLES_USERNAME="",
            OPENSUBTITLES_PASSWORD="",
        )

    if changes:
        config.save_settings(changes)
        svc.apply_keys()
        logger.info("API keys updated: %s", ", ".join(sorted(changes)))
    return jsonify(success=True, **_key_status())


@api.route("/admin/login", methods=["POST"])
def admin_login():
    body = _body()
    ok, error = auth.login(
        str(body.get("username") or ""), str(body.get("password") or "")
    )
    if not ok:
        return jsonify(success=False, error=error), 401
    return jsonify(success=True, default_password=auth.using_default())


@api.route("/admin/logout", methods=["POST"])
def admin_logout():
    auth.logout()
    return jsonify(success=True)


@api.route("/admin/credentials", methods=["POST"])
@admin_required
def admin_credentials():
    """Body: {current_password, username?, new_password}"""
    body = _body()
    current = str(body.get("current_password") or "")
    new_username = str(body.get("username") or "").strip() or auth.username()
    new_password = str(body.get("new_password") or "")
    if not auth.verify(auth.username(), current):
        return jsonify(success=False, error=t("password_current_wrong")), 400
    if len(new_password) < 8:
        return jsonify(success=False, error=t("password_too_short")), 400
    if new_password == auth.DEFAULT_PASSWORD:
        return jsonify(success=False, error=t("password_is_default")), 400
    auth.change_credentials(new_username, new_password)
    logger.info("Admin credentials changed")
    return jsonify(success=True, username=new_username)


@api.route("/healthz")
def healthz():
    return "ok"
