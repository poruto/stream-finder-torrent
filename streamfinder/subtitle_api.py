"""Subtitles API: finding them for a playing file, WebVTT (also machine-translated),
syncing a subtitle to the speech, and AI subtitles made from the audio."""

import json
import logging
from dataclasses import replace

from flask import Blueprint, Response, jsonify, request, stream_with_context

from .api_helpers import (
    HASH_RE,
    as_float,
    as_int,
    json_body,
    parse_media_type,
    torrent_file,
    torrserver_error,
)
from .caching import LRU
from .i18n import t
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
from .subtitles.transcribe import OVERLAP, TranscribeError, _language_code
from .torrserver import TorrServerError
from .views import year_of

logger = logging.getLogger(__name__)
subtitle_api = Blueprint("subtitle_api", __name__, url_prefix="/api")


@subtitle_api.route("/subtitles/find", methods=["POST"])
def find_subtitles():
    """Body: {hash, file_index, tmdb_id, media_type, season?, episode?, languages?}"""
    body = json_body()
    info_hash = (body.get("hash") or "").lower()
    if not HASH_RE.fullmatch(info_hash):
        return jsonify(success=False, error=t("err_missing_hash")), 400
    media_type = parse_media_type(body.get("media_type"))
    tmdb_id = as_int(body.get("tmdb_id"))
    season, episode = as_int(body.get("season")), as_int(body.get("episode"))
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
            as_int(body.get("file_index")),
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
        return torrserver_error(e)
    return jsonify(
        success=True,
        ai=svc.transcriber.available,
        translators=svc.translator.engines(),
        **result,
    )


@subtitle_api.route("/subtitles/ai/<info_hash>/<int:index>")
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
    file = torrent_file(info_hash, index)
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


@subtitle_api.route("/subtitles/sync", methods=["POST"])
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
    body = json_body()
    svc = services()
    if not svc.transcoder.available:
        return jsonify(success=False, error="ffmpeg is not installed"), 501
    info_hash = (body.get("hash") or "").lower()
    file = torrent_file(info_hash, as_int(body.get("file_index")) or 0)
    url = svc.torrserver.stream_url(info_hash, file)
    position = max(0.0, as_float(body.get("position")))
    audio_track = max(0, as_int(body.get("audio")) or 0)
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


@subtitle_api.route("/subtitles/<candidate_id>/translated")
def subtitle_translated_window(candidate_id: str):
    """Machine-translated lines of one stretch of a subtitle, for the player to show
    while it plays. Query: translate (language code), engine (google|ai), start,
    duration (seconds of the subtitle's own timing). Returns {lines: [{start, end, text}]}."""
    svc = services()
    target = (request.args.get("translate") or "").lower()[:2]
    if not (target.isalpha() and len(target) == 2):
        return jsonify(success=False, error="translate: a language code"), 400
    try:
        lines = svc.subtitles.translated_window(
            candidate_id,
            target,
            "ai" if request.args.get("engine") == "ai" else "google",
            svc.translator,
            max(0.0, request.args.get("start", 0, type=float)),
            min(600.0, max(5.0, request.args.get("duration", 60, type=float))),
        )
    except ValueError as e:
        return jsonify(success=False, error=str(e)), 404
    except Exception as e:  # network / quota errors from providers
        logger.warning("Subtitle %s translation failed: %s", candidate_id, e)
        return jsonify(success=False, error=str(e)), 502
    return jsonify(success=True, lines=lines)


@subtitle_api.route("/subtitles/<candidate_id>.vtt")
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
