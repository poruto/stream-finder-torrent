"""JSON API used by the frontend."""

import json
import logging
import re
import threading
import time
import unicodedata
from dataclasses import replace
from typing import Any
from urllib.parse import quote, urlparse

import requests
from flask import Blueprint, Response, abort, jsonify, request, stream_with_context

from . import auth, config
from .auth import admin_required
from .caching import LRU
from .i18n import t
from .matching import normalize_title
from .services import services
from .subtitles.sync import (
    align,
    confirm,
    fit_pairs,
    match_words,
    merge,
    nearest,
    spoken_words,
)
from .subtitles.transcribe import (
    LOCAL_URL,
    OVERLAP,
    TranscribeError,
    _language_code,
)
from .subtitles.whisper_models import MODELS as WHISPER_MODELS
from .tmdb import TMDBError, match_titles
from .torrents import SearchQuery
from .torrserver import (
    READ_AHEAD,
    TorrServerError,
    normalize_magnet,
    select_video_file,
    video_files,
)
from .transcode import TranscoderBusy
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


def _float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


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
    """Body: {tmdb_id, media_type, season?, episode?, runtime?, quality?, hevc?, query?};
    `query` = free text, quality "saver" = smallest good version, `hevc` = the browser decodes HEVC."""
    query = search_query(_body())
    if query is None:
        return jsonify(success=False, error=t("err_missing_query")), 400
    return jsonify(success=True, **services().torrents.search(query))


def search_query(body: dict) -> SearchQuery | None:
    """The torrent search for a /torrents body (also used by the new-episode notifier)."""
    media_type = _media_type(body.get("media_type"))
    tmdb_id = _int(body.get("tmdb_id"))
    free_text = (body.get("query") or "").strip()
    season, episode = _int(body.get("season")), _int(body.get("episode"))
    runtime = _int(body.get("runtime"))
    quality = _int(body.get("quality"))
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
    if match := re.search(r"btih:([a-fA-F0-9]{40})", magnet):
        extra = services().torrents.extra_trackers(match.group(1))
        magnet += "".join(f"&tr={quote(url, safe='')}" for url in extra)
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


@api.route("/preload", methods=["POST"])
def preload():
    """Body: {magnet, season?, episode?, title?, poster?} - the next episode, while the
    current one plays: added to TorrServer at once, its file's start downloaded in the
    background, so /play starts it without waiting. Answers with the hash right away."""
    body = _body()
    magnet = normalize_magnet(body.get("magnet", ""))
    if not magnet:
        return jsonify(success=False, error=t("err_bad_magnet")), 400
    torrserver = services().torrserver
    try:
        info_hash = torrserver.add(
            magnet, title=body.get("title") or "", poster=body.get("poster") or ""
        )
    except TorrServerError as e:
        return _torrserver_error(e)
    season, episode = _int(body.get("season")), _int(body.get("episode"))

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


def _torrent_file(info_hash: str, index: int):
    """The TorrentFile for a stream, or 404."""
    if not _HASH_RE.fullmatch(info_hash):
        abort(404)
    try:
        file = services().torrserver.file(info_hash, index, wait=10)
    except TorrServerError:
        abort(502)
    if file is None or not file.is_video:
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


@api.route("/torrent-stop/<info_hash>", methods=["POST"])
def torrent_stop(info_hash: str):
    """The player moved on (other version, closed): stop downloading this torrent."""
    if not _HASH_RE.fullmatch(info_hash):
        abort(404)
    try:
        services().torrserver.drop(info_hash)
    except TorrServerError as e:
        return _torrserver_error(e)
    return jsonify(success=True)


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
    return jsonify(
        success=True,
        ai=svc.transcriber.available,
        translators=svc.translator.engines(),
        **result,
    )


@api.route("/subtitles/ai/<info_hash>/<int:index>")
def ai_subtitles(info_hash: str, index: int):
    """AI subtitles for one window of the playing file, made from its audio.

    Query: start, duration (seconds of the file), audio (track number),
    translate (language code, empty = the spoken language), language (spoken language if known),
    via (english = Whisper translates the speech into English, Google into `translate`;
    direct = transcribe in the spoken language, then translate that).
    """
    svc = services()
    if not svc.transcriber.available:
        return jsonify(success=False, error=t("ai_subs_unavailable")), 501
    file = _torrent_file(info_hash, index)
    start = max(0.0, request.args.get("start", 0, type=float))
    duration = request.args.get("duration", 30, type=float)
    translate_to = (request.args.get("translate") or "").lower()[:2]
    language = (request.args.get("language") or "").lower()[:2]
    try:
        result = svc.transcriber.window(
            svc.torrserver.stream_url(info_hash, file),
            start,
            duration,
            audio_track=max(0, request.args.get("audio", 0, type=int)),
            translate_to=translate_to if translate_to.isalpha() else "",
            language=language if language.isalpha() else "",
            engine="ai" if request.args.get("engine") == "ai" else "google",
            via="direct" if request.args.get("via") == "direct" else "english",
        )
    except TranscribeError as e:
        logger.info("AI subtitles failed at %.0f s: %s", start, e)
        response = jsonify(success=False, error=str(e), retry_after=e.retry_after)
        return response, e.status
    return jsonify(success=True, **result)


@api.route("/subtitles/sync", methods=["POST"])
def sync_subtitles():
    """Align a subtitle to the speech around `position`.

    Body: {hash, file_index, subtitle (candidate id), position, audio?}
    Returns {pieces, offset, scale, certain, changed, applied, method}: a cue at
    subtitle time t belongs at t * scale + offset of the piece it falls in (between
    pieces, on the straight line from one to the next); `offset`/`scale` are the
    ones at `position`. Measurements add up per subtitle, so syncing again later
    in the film can fix another cut. Only a `certain` measurement changes anything.

    With `Accept: application/x-ndjson` the progress comes first, one JSON object
    per line ({"stage": "listening", "step": 1, "steps": 5}, {"stage": "matching"}),
    and the last line is the result with "done": true.
    """
    body = _body()
    svc = services()
    if not svc.transcoder.available:
        return jsonify(success=False, error="ffmpeg is not installed"), 501
    info_hash = (body.get("hash") or "").lower()
    file = _torrent_file(info_hash, _int(body.get("file_index")) or 0)
    url = svc.torrserver.stream_url(info_hash, file)
    position = max(0.0, _float(body.get("position")))
    audio_track = max(0, _int(body.get("audio")) or 0)
    candidate = str(body.get("subtitle") or "")
    try:
        cues = svc.subtitles.cues(candidate)
    except ValueError as e:
        return jsonify(success=False, error=str(e)), 404
    except Exception as e:  # download failure
        logger.info("subtitle sync failed: %s", e)
        return jsonify(success=False, error=str(e)), 502

    steps = _sync_steps(svc, url, candidate, cues, position, audio_track)
    if "application/x-ndjson" in request.headers.get("Accept", ""):
        lines = (json.dumps(step) + "\n" for step in steps)
        return Response(
            stream_with_context(lines),
            mimetype="application/x-ndjson",
            # Proxies must pass each line on at once, not when the sync is over.
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    *_, result = steps
    status = result.pop("status", 200)
    return jsonify(result), status


SYNC_WINDOW = 30  # seconds of audio per transcript window
# Pieces measured so far per (stream, subtitle, audio track): a few films' worth.
_sync_maps = LRU(32)
# Word-timed transcripts per (stream, start, audio track): syncing again costs nothing.
_heard = LRU(240)


def _sync_steps(svc, url, candidate, cues, position, audio_track):
    """Progress of one subtitle sync, then its result (dicts for the player)."""
    key = (url, candidate, audio_track)
    known = _sync_maps.get(key) or []
    result = None
    if svc.transcriber.available:
        try:
            result = yield from _sync_by_text(
                svc,
                url,
                cues,
                svc.subtitles.language_of(candidate),
                position,
                audio_track,
            )
        except TranscribeError as e:  # fall back to speech detection
            logger.info("text-based subtitle sync failed: %s", e)
    if result is None:
        try:
            result = yield from _sync_by_speech(
                svc, url, [(s, e) for s, e, _ in cues], position
            )
        except Exception as e:  # ffmpeg timeout, slow torrent
            logger.info("subtitle sync failed: %s", e)
            yield {"done": True, "success": False, "error": str(e), "status": 502}
            return
    if result is None:
        yield {
            "done": True,
            "success": False,
            "error": "no speech found",
            "status": 422,
        }
        return

    certain = bool(result.pieces) and bool(result.applied or result.details.get("sure"))
    pieces = known
    if certain:
        for piece in result.pieces:
            pieces = merge(pieces, piece)
        _sync_maps[key] = pieces
    here = nearest(pieces, position)
    yield {
        "done": True,
        "success": True,
        **result.to_dict(),
        "pieces": [p.to_dict() for p in pieces],
        "offset": round(here.offset, 2) if here else 0.0,
        "scale": round(here.scale, 6) if here else 1.0,
        "applied": any(p.offset or p.scale != 1.0 for p in pieces),
        "certain": certain,
        "changed": pieces != known,
    }


def _transcript(svc, url, start, audio_track):
    """(words with their times, spoken language) of [start, start + SYNC_WINDOW)."""
    key = (url, start, audio_track)
    cached = _heard.get(key)
    if cached is not None:
        return cached
    clip = max(0.0, start - OVERLAP)  # whole words at the edges, as for AI subtitles
    audio = svc.transcriber.extract_audio(
        url, clip, SYNC_WINDOW + (start - clip) + OVERLAP, audio_track
    )
    if len(audio) < 2000:  # nothing decodable here (end of the file)
        result = ([], None)
    else:
        data = svc.transcriber.transcribe(audio)
        words = [
            w for w in spoken_words(data, clip) if start <= w[0] < start + SYNC_WINDOW
        ]
        result = (words, _language_code(str(data.get("language") or "")) or None)
    _heard[key] = result
    return result


def _sync_by_text(svc, url, cues, sub_language, position, audio_track):
    """Match subtitle lines with the words said around `position`; None = can't decide this way.

    Three windows (90 s) first; when they aren't enough, two more.
    """
    first = int(max(0.0, position - 45) // SYNC_WINDOW)
    plan = [k for k in (first, first + 1, first + 2, first + 3, first - 1) if k >= 0]
    words, spoken = [], None
    for n, k in enumerate(plan, 1):
        yield {"stage": "listening", "step": n, "steps": len(plan)}
        heard, language = _transcript(svc, url, k * SYNC_WINDOW, audio_track)
        spoken = spoken or language
        if spoken and sub_language and spoken != sub_language:
            return None  # other language: only the timing of speech can help
        words += heard
        if n < 3:
            continue
        yield {"stage": "matching"}
        result = fit_pairs(match_words(cues, words))
        if result.details.get("sure"):
            return result
    return None


def _sync_by_speech(svc, url, cues, position):
    """Lay cues over detected speech; confirmed on a second stretch of the film."""
    start = max(0.0, position - 30)
    yield {"stage": "listening", "step": 1, "steps": 2}
    speech = svc.transcoder.speech(url, start)
    if not speech:
        return None
    yield {"stage": "matching"}
    result = align(cues, speech, (start, start + 120))
    if not result.applied:
        return result
    # An earlier stretch is already downloaded; at the very start look ahead instead.
    other = start - 150 if start >= 150 else start + 120
    yield {"stage": "listening", "step": 2, "steps": 2}
    speech = svc.transcoder.speech(url, other)
    if not speech:
        return replace(
            result, applied=False
        )  # nothing to confirm it with: change nothing
    yield {"stage": "matching"}
    return confirm(result, align(cues, speech, (other, other + 120)))


@api.route("/subtitles/<candidate_id>.vtt")
def subtitle_vtt(candidate_id: str):
    """A subtitle as WebVTT; ?translate=cs&engine=google|ai for a machine translation."""
    svc = services()
    target = (request.args.get("translate") or "").lower()[:2]
    engine = "ai" if request.args.get("engine") == "ai" else "google"
    try:
        if target.isalpha() and len(target) == 2:
            vtt = svc.subtitles.translated_vtt(
                candidate_id, target, engine, svc.translator
            )
        else:
            vtt = svc.subtitles.get_vtt(candidate_id)
    except ValueError as e:
        return Response(str(e), 404, mimetype="text/plain")
    except Exception as e:  # network / quota errors from providers
        logger.warning("Subtitle %s failed: %s", candidate_id, e)
        return Response(str(e), 502, mimetype="text/plain")
    response = Response(
        vtt, mimetype="text/vtt", headers={"Cache-Control": "private, max-age=86400"}
    )
    # Reopening a title asks again: "304 Not Modified" instead of the whole file.
    response.add_etag()
    return response.make_conditional(request)


def _key_status() -> dict:
    return {
        "tmdb": bool(config.TMDB_API_KEY),
        "opensubtitles": bool(config.OPENSUBTITLES_API_KEY),
        "opensubtitles_login": bool(
            config.OPENSUBTITLES_USERNAME and config.OPENSUBTITLES_PASSWORD
        ),
        "transcribe": services().transcriber.available,
        "transcribe_enabled": config.flag(config.TRANSCRIBE_ENABLED),
        "whisper_quality": services().whisper.current(),
        "whisper_state": services().whisper.state,
        "whisper_local": services().whisper.local,
        "transcribe_key": bool(config.TRANSCRIBE_API_KEY),
        "transcribe_local": config.TRANSCRIBE_API_URL,  # an address on the LAN, not a secret
        "transcribe_provider": services().transcriber.provider,
        "sktorrent": bool(config.SKTORRENT_USERNAME and config.SKTORRENT_PASSWORD),
        "torrserver_upload": config.TORRSERVER_UPLOAD_LIMIT or "0",
        "torrserver_read_ahead": config.TORRSERVER_READ_AHEAD or "normal",
    }


@api.route("/keys")
@admin_required
def keys_status():
    """Which keys are configured. The keys themselves are never sent back."""
    return jsonify(success=True, **_key_status())


@api.route("/keys", methods=["POST"])
@admin_required
def keys_update():
    """Body: any of {tmdb_api_key, opensubtitles_api_key, opensubtitles_username, opensubtitles_password,
    transcribe_api_key, torrserver_upload, torrserver_read_ahead} plus optional
    remove: ["opensubtitles", "transcribe"].
    Every new key is tested before it is saved."""
    body = _body()
    svc = services()
    value = lambda name: (
        (body.get(name) or "").strip() if isinstance(body.get(name), str) else ""
    )
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

    # AI subtitles: where speech is transcribed ("local" Whisper server or "api" key),
    # the Groq/OpenAI key and the local server's address. Each is checked before saving.
    provider = value("transcribe_provider")
    key, url = value("transcribe_api_key"), value("transcribe_api_url").rstrip("/")
    if url and not re.fullmatch(r"https?://\S+", url):
        return jsonify(success=False, field="transcribe", error=t("url_invalid")), 400
    try:
        if key and not svc.transcriber.check_key(key):
            return jsonify(
                success=False, field="transcribe", error=t("key_invalid_speech")
            ), 400
        if key:
            changes["TRANSCRIBE_API_KEY"] = key
        if provider == "local" or url:
            local = url or config.TRANSCRIBE_API_URL or LOCAL_URL
            if not svc.transcriber.check_local(local):
                return jsonify(
                    success=False,
                    field="transcribe",
                    error=t("whisper_unreachable", url=local),
                ), 400
            changes["TRANSCRIBE_API_URL"] = local
        if provider == "api" and not (key or config.TRANSCRIBE_API_KEY):
            return jsonify(
                success=False, field="transcribe", error=t("key_needed_api")
            ), 400
    except requests.RequestException:
        return jsonify(
            success=False, field="transcribe", error=t("key_unreachable", name="AI")
        ), 502
    quality = value("whisper_quality")
    if quality in WHISPER_MODELS and quality != svc.whisper.current():
        svc.whisper.select(quality, config.TRANSCRIBE_API_URL or LOCAL_URL)

    if body.get("transcribe_enabled") in ("1", "0", True, False):
        enable = body["transcribe_enabled"] in ("1", True)
        changes["TRANSCRIBE_ENABLED"] = "true" if enable else "false"
        source = (
            provider
            or config.TRANSCRIBE_PROVIDER
            or ("api" if key or config.TRANSCRIBE_API_KEY else "local")
        )
        if enable and source == "api" and not (key or config.TRANSCRIBE_API_KEY):
            return jsonify(
                success=False, field="transcribe", error=t("key_needed_api")
            ), 400
        if enable and source == "local" and "TRANSCRIBE_API_URL" not in changes:
            local = config.TRANSCRIBE_API_URL or LOCAL_URL
            if not svc.transcriber.check_local(local):
                return jsonify(
                    success=False,
                    field="transcribe",
                    error=t("whisper_unreachable", url=local),
                ), 400
            changes["TRANSCRIBE_API_URL"] = local
        if enable and not provider:
            changes["TRANSCRIBE_PROVIDER"] = source
    if provider in ("local", "api"):
        changes["TRANSCRIBE_PROVIDER"] = provider
    elif key or url:
        changes["TRANSCRIBE_PROVIDER"] = "api" if key and not url else "local"

    sk_user, sk_password = value("sktorrent_username"), value("sktorrent_password")
    if sk_user and sk_password:
        try:
            if not svc.torrents.sktorrent.check_login(sk_user, sk_password):
                return jsonify(
                    success=False, field="sktorrent", error=t("sk_login_invalid")
                ), 400
        except requests.RequestException:
            return jsonify(
                success=False,
                field="sktorrent",
                error=t("key_unreachable", name="SkTorrent"),
            ), 502
        changes.update(SKTORRENT_USERNAME=sk_user, SKTORRENT_PASSWORD=sk_password)
    if "sktorrent" in (body.get("remove") or []):
        changes.update(SKTORRENT_USERNAME="", SKTORRENT_PASSWORD="")

    if "transcribe" in (body.get("remove") or []):
        changes.update(
            TRANSCRIBE_API_KEY="",
            TRANSCRIBE_API_URL="",
            TRANSCRIBE_PROVIDER="",
            TRANSCRIBE_ENABLED="false",
        )

    if "opensubtitles" in (body.get("remove") or []):
        changes.update(
            OPENSUBTITLES_API_KEY="",
            OPENSUBTITLES_USERNAME="",
            OPENSUBTITLES_PASSWORD="",
        )

    # Network use: TorrServer's upload limit (kB/s, "0" = unlimited, "off") and read-ahead.
    # Only real changes are saved, so TorrServer's own settings stay until one is picked.
    upload = value("torrserver_upload").lower()
    if upload:
        if upload != "off" and not upload.isdigit():
            return jsonify(
                success=False, field="torrserver", error=t("upload_invalid")
            ), 400
        upload = upload if upload == "off" else str(int(upload))
        if upload != (config.TORRSERVER_UPLOAD_LIMIT or "0"):
            changes["TORRSERVER_UPLOAD_LIMIT"] = upload
    read_ahead = value("torrserver_read_ahead")
    if read_ahead in READ_AHEAD and read_ahead != (
        config.TORRSERVER_READ_AHEAD or "normal"
    ):
        changes["TORRSERVER_READ_AHEAD"] = read_ahead

    if changes:
        config.save_settings(changes)
        svc.apply_keys()
        logger.info("API keys updated: %s", ", ".join(sorted(changes)))
    status = _key_status()
    network = {"TORRSERVER_UPLOAD_LIMIT", "TORRSERVER_READ_AHEAD"} & changes.keys()
    if network and not svc.apply_network(attempts=1):
        svc.apply_network_later()  # TorrServer is down: as soon as it is back
        status["notice"] = "torrserver_pending"
    return jsonify(success=True, **status)


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
