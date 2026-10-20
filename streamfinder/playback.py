"""Playback API: the torrent search, starting a stream in TorrServer, warming up the
next episode, and the converted (ffmpeg) streams for files the browser can't play."""

import logging
import re
import threading
import time
import unicodedata
from urllib.parse import quote, urlparse

import requests
from flask import Blueprint, Response, abort, jsonify, request

from . import config
from .api_helpers import (
    HASH_RE,
    as_int,
    json_body,
    parse_media_type,
    torrent_file,
    torrserver_error,
)
from .i18n import t
from .matching import normalize_title
from .services import services
from .tmdb import match_titles
from .torrents import SearchQuery
from .torrserver import (
    TorrServerError,
    normalize_magnet,
    select_video_file,
    video_files,
)
from .transcode import TranscoderBusy
from .views import year_of

logger = logging.getLogger(__name__)
playback = Blueprint("playback", __name__, url_prefix="/api")


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


@playback.route("/torrents", methods=["POST"])
def torrents():
    """Body: {tmdb_id, media_type, season?, episode?, runtime?, quality?, hevc?, query?};
    `query` = free text, quality "saver" = smallest good version, `hevc` = the browser decodes HEVC."""
    query = search_query(json_body())
    if query is None:
        return jsonify(success=False, error=t("err_missing_query")), 400
    return jsonify(success=True, **services().torrents.search(query))


def search_query(body: dict) -> SearchQuery | None:
    """The torrent search for a /torrents body (also used by the new-episode notifier)."""
    media_type = parse_media_type(body.get("media_type"))
    tmdb_id = as_int(body.get("tmdb_id"))
    free_text = (body.get("query") or "").strip()
    season, episode = as_int(body.get("season")), as_int(body.get("episode"))
    runtime = as_int(body.get("runtime"))
    quality = as_int(body.get("quality"))
    quality = quality if quality in (2160, 1080, 720) else None
    saver = body.get("quality") == "saver"

    data = services().tmdb.details(media_type, tmdb_id) if tmdb_id else {}
    # Episode counts let packs be judged by size per episode, not total size.
    season_counts = {
        s["season_number"]: s.get("episode_count") or 0
        for s in data.get("seasons") or []
        if s.get("season_number") is not None
    }
    common = dict(
        czech=bool(body.get("czech")),
        media_type=media_type,
        season=season,
        episode=episode,
        quality=quality,
        saver=saver,
        hevc=saver and bool(body.get("hevc")),
        season_episodes=season_counts,
        total_episodes=data.get("number_of_episodes"),
        runtime=runtime
        or data.get("runtime")
        or (data.get("episode_run_time") or [None])[0],
    )
    if free_text:
        return SearchQuery(
            title=free_text, titles=[free_text], free_text=True, **common
        )
    if tmdb_id:
        titles = match_titles(data)
        title = data.get("english_title") or titles[0]
        local = [data.get("czech_title")]
        original = data.get("original_title") or data.get("original_name")
        if data.get("original_language") in ("cs", "sk"):
            local.append(original)
        return SearchQuery(
            title=title,
            titles=titles,
            year=year_of(data),
            imdb_id=data.get("imdb_id"),
            local_titles=_search_titles(local, title),
            # Remakes and same-name shows need the year / IMDb id to match.
            ambiguous=services().tmdb.has_namesake(
                media_type, tmdb_id, [title, original]
            ),
            countries=list(data.get("origin_country") or [])
            if media_type == "tv"
            else [],
            **common,
        )
    return None


def _search_titles(titles: list[str | None], main: str) -> list[str]:
    """Extra titles to search by, without diacritics ("Pelíšky" -> "Pelisky") like release names."""
    seen = {normalize_title(main)}
    result = []
    for title in titles:
        if not title:
            continue
        plain = unicodedata.normalize("NFKD", title)
        plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
        if normalize_title(plain) not in seen:
            seen.add(normalize_title(plain))
            result.append(plain)
    return result


@playback.route("/play", methods=["POST"])
def play():
    """Add a torrent to TorrServer and pick the file to stream.

    Body: {magnet, season?, episode?, file_index?, title?, poster?}
    """
    body = json_body()
    magnet = normalize_magnet(body.get("magnet", ""))
    if not magnet:
        return jsonify(success=False, error=t("err_bad_magnet")), 400
    torrserver = services().torrserver
    if match := re.search(r"btih:([a-fA-F0-9]{40})", magnet):
        extra = services().torrents.extra_trackers(match.group(1))
        magnet += "".join(f"&tr={quote(url, safe='')}" for url in extra)
    try:
        info_hash = torrserver.add(
            magnet, title=body.get("title") or "", poster=body.get("poster") or ""
        )
        files = torrserver.files(info_hash, wait=45)
    except TorrServerError as e:
        return torrserver_error(e)
    if not files:
        return jsonify(
            success=False, retry=True, hash=info_hash, error=t("err_no_seeds")
        ), 504

    season, episode = as_int(body.get("season")), as_int(body.get("episode"))
    file_index = as_int(body.get("file_index"))

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
            return torrserver_error(e)
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


@playback.route("/preload", methods=["POST"])
def preload():
    """Body: {magnet, season?, episode?, title?, poster?} - the next episode, while the
    current one plays: added to TorrServer at once, its file's start downloaded in the
    background, so /play starts it without waiting. Answers with the hash right away."""
    body = json_body()
    magnet = normalize_magnet(body.get("magnet", ""))
    if not magnet:
        return jsonify(success=False, error=t("err_bad_magnet")), 400
    torrserver = services().torrserver
    try:
        info_hash = torrserver.add(
            magnet, title=body.get("title") or "", poster=body.get("poster") or ""
        )
    except TorrServerError as e:
        return torrserver_error(e)
    season, episode = as_int(body.get("season")), as_int(body.get("episode"))

    def warm_up():
        try:
            file = select_video_file(
                torrserver.files(info_hash, wait=60), season, episode
            )
            if file is not None:
                torrserver.preload(info_hash, file)
        except (TorrServerError, requests.RequestException) as e:
            logger.info("preloading %s failed: %s", info_hash, e)

    threading.Thread(target=warm_up, daemon=True).start()
    return jsonify(success=True, hash=info_hash)


@playback.route("/probe/<info_hash>/<int:index>")
def probe(info_hash: str, index: int):
    """Codecs of a playing file, so the player knows whether audio needs converting."""
    svc = services()
    if not svc.transcoder.available:
        return jsonify(success=True, available=False)
    file = torrent_file(info_hash, index)
    try:
        result = svc.transcoder.probe(svc.torrserver.stream_url(info_hash, file))
    except Exception as e:  # ffprobe timeout / unreadable stream
        logger.info("probe failed for %s: %s", file.name, e)
        return jsonify(success=True, available=True, probed=False)
    return jsonify(success=True, available=True, probed=True, **result.to_dict())


@playback.route("/keyframe/<info_hash>/<int:index>")
def keyframe(info_hash: str, index: int):
    """Exact start time of a converted stream that is asked to begin at ?t=."""
    svc = services()
    if not svc.transcoder.available:
        abort(404)
    file = torrent_file(info_hash, index)
    position = max(0.0, request.args.get("t", 0, type=float))
    try:
        start = svc.transcoder.keyframe_at(
            svc.torrserver.stream_url(info_hash, file), position
        )
    except Exception as e:  # timeout: fall back to the requested time
        logger.info("keyframe lookup failed: %s", e)
        start = position
    return jsonify(success=True, start=start)


@playback.route("/transcode/<info_hash>/<int:index>.mp4")
def transcode(info_hash: str, index: int):
    """Video copied as-is, audio converted to AAC, streamed as fragmented MP4.

    Query: start (seconds), audio (track number), copy=1 to only remux (Matroska -> MP4).
    """
    svc = services()
    if not svc.transcoder.available:
        abort(404)
    file = torrent_file(info_hash, index)
    url = svc.torrserver.stream_url(info_hash, file)
    start = max(0.0, request.args.get("start", 0, type=float))
    track = max(0, request.args.get("audio", 0, type=int))
    try:
        video_codec = svc.transcoder.probe(url).video_codec
    except Exception:
        video_codec = None
    try:
        chunks = svc.transcoder.stream(
            url,
            start,
            track,
            copy_audio=request.args.get("copy") == "1",
            video_codec=video_codec,
            viewer=request.args.get("sid", "")[:64],  # one conversion per player
        )
    except TranscoderBusy as e:
        return jsonify(success=False, error=str(e)), 503
    return Response(
        chunks,  # needs no request context; its close() always reaches ffmpeg
        mimetype="video/mp4",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@playback.route("/torrent-stats/<info_hash>")
def torrent_stats(info_hash: str):
    if not HASH_RE.fullmatch(info_hash):
        abort(404)
    try:
        status = services().torrserver.get(info_hash) or {}
    except TorrServerError as e:
        return torrserver_error(e)
    return jsonify(
        success=True,
        seeders=status.get("connected_seeders", 0),
        peers=status.get("active_peers", 0),
        download_speed=int(status.get("download_speed") or 0),
    )


@playback.route("/torrent-stop/<info_hash>", methods=["POST"])
def torrent_stop(info_hash: str):
    """The player moved on (other version, closed): stop downloading this torrent."""
    if not HASH_RE.fullmatch(info_hash):
        abort(404)
    try:
        services().torrserver.drop(info_hash)
    except TorrServerError as e:
        return torrserver_error(e)
    return jsonify(success=True)
