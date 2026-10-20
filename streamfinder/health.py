"""Health checks for the Status page: every service StreamFinder depends on, in one place.

Each check answers {id, group, name, state, detail, ms}: state "ok", "warn" (works,
but worth a look), "fail" or "off" (optional and not set up). They run in parallel
with short timeouts and the result is kept for a few seconds, so opening the page
twice doesn't ask every API twice. Keys never appear in the answer.
"""

import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from urllib.parse import urlparse

import requests
from flask import Blueprint, copy_current_request_context, jsonify, request

from . import config, version
from .i18n import t
from .services import services
from .subtitles.translate import GOOGLE_URL
from .tmdb import TMDBError

health = Blueprint("health", __name__)

KEEP_FOR = 300  # seconds a result is reused ("Check again" asks anew)
STARTED = time.time()
LOW_DISK = 2 * 1024**3
_cache: dict = {}
_lock = threading.Lock()

GROUPS = ("streaming", "catalogue", "subtitles", "ai", "notifications", "system")


def _result(state: str, detail: str = "") -> dict:
    return {"state": state, "detail": detail}


def _http(url: str, **kwargs) -> requests.Response:
    return requests.get(url, timeout=kwargs.pop("timeout", 6), **kwargs)


def _host(url: str) -> str:
    return urlparse(url).netloc or url


# -- the checks ----------------------------------------------------------------------


def check_torrserver(svc) -> dict:
    try:
        response = svc.torrserver.session.get(
            f"{svc.torrserver.base_url}/echo", timeout=4
        )
    except requests.RequestException:
        return _result(
            "fail", t("health_unreachable", host=_host(svc.torrserver.base_url))
        )
    if response.status_code != 200:
        return _result("fail", t("health_http", code=response.status_code))
    return _result(
        "ok", f"{response.text.strip()[:40]} · {_host(svc.torrserver.base_url)}"
    )


def check_ffmpeg(svc) -> dict:
    ffmpeg = svc.transcoder.ffmpeg
    if not ffmpeg or not svc.transcoder.available:
        return _result("warn", t("health_ffmpeg_missing"))
    try:
        out = subprocess.run(
            [ffmpeg, "-version"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return _result("fail", t("health_ffmpeg_missing"))
    first = (out.splitlines() or [""])[0]
    return _result("ok", " ".join(first.split()[:3]))


def check_tmdb(svc) -> dict:
    if not config.TMDB_API_KEY:
        return _result("fail", t("health_key_missing"))
    try:
        ok = svc.tmdb.check_key(config.TMDB_API_KEY)
    except TMDBError:
        return _result("fail", t("health_unreachable", host="api.themoviedb.org"))
    return _result(
        "ok" if ok else "fail", t("health_ok") if ok else t("health_key_rejected")
    )


def check_imdb(svc) -> dict:
    if not svc.imdb.ready:
        return _result("warn", t("imdb_loading"))
    return _result("ok", t("imdb_ready", n=f"{svc.imdb.count:,}".replace(",", " ")))


def check_opensubtitles(svc) -> dict:
    if not config.OPENSUBTITLES_API_KEY:
        return _result("off", t("health_optional_key"))
    try:
        ok = svc.subtitles.api.check_key(config.OPENSUBTITLES_API_KEY)
    except requests.RequestException:
        return _result("fail", t("health_unreachable", host="api.opensubtitles.com"))
    login = (
        t("health_logged_in") if config.OPENSUBTITLES_USERNAME else t("health_no_login")
    )
    return _result("ok", login) if ok else _result("fail", t("health_key_rejected"))


def check_opensubtitles_org(svc) -> dict:
    url = "https://www.opensubtitles.org"
    try:
        response = _http(url, headers={"User-Agent": "StreamFinder"})
    except requests.RequestException:
        return _result("fail", t("health_unreachable", host=_host(url)))
    if response.status_code >= 500:
        return _result("fail", t("health_http", code=response.status_code))
    return _result("ok", t("health_ok"))


def check_translation(svc) -> dict:
    # Only whether the server answers: a test translation every few minutes would use
    # up the free quota the real subtitles need.
    host = _host(GOOGLE_URL)
    try:
        response = requests.head(f"https://{host}/", timeout=6)
    except requests.RequestException:
        return _result("fail", t("health_unreachable", host=host))
    if response.status_code >= 500:
        return _result("warn", t("health_http", code=response.status_code))
    engine = t("engine_ai") if svc.transcriber.chat_available else t("engine_google")
    return _result("ok", t("health_translation", engine=engine))


def check_whisper(svc) -> dict:
    url = svc.transcriber.local_url or config.TRANSCRIBE_API_URL
    if not url:
        return _result("off", t("health_whisper_off"))
    started = time.monotonic()
    if not svc.transcriber.check_local(url):
        return _result("fail", t("health_unreachable", host=_host(url)))
    quality = svc.whisper.current() if svc.whisper.local else ""
    detail = t(
        "health_whisper_ok", quality=t(f"whisper_quality_{quality}") if quality else "-"
    )
    slow = time.monotonic() - started > 2
    return _result("warn" if slow else "ok", detail)


def check_speech_api(svc) -> dict:
    key = config.TRANSCRIBE_API_KEY
    if not key:
        return _result("off", t("health_optional_key"))
    try:
        ok = svc.transcriber.check_key(key)
    except requests.RequestException:
        return _result("fail", t("health_unreachable", host="api.groq.com"))
    return (
        _result("ok", t("health_ok"))
        if ok
        else _result("fail", t("health_key_rejected"))
    )


def check_ai_subtitles(svc) -> dict:
    on = config.flag(config.TRANSCRIBE_ENABLED)
    if not on:
        return _result("off", t("health_ai_off"))
    if not svc.transcriber.available:
        return _result("fail", t("health_ai_no_engine"))
    provider = (
        t("health_ai_local")
        if svc.transcriber.provider == "local"
        else t("health_ai_api")
    )
    return _result("ok", provider)


def check_ntfy(svc) -> dict:
    url = (getattr(config, "NOTIFY_NTFY_URL", "") or "").strip()
    if not url:
        return _result("off", t("health_notify_off"))
    parsed = urlparse(url)
    try:
        response = _http(f"{parsed.scheme}://{parsed.netloc}/v1/health")
    except requests.RequestException:
        return _result("fail", t("health_unreachable", host=parsed.netloc))
    ok = response.ok and (
        response.json() if "json" in response.headers.get("Content-Type", "") else {}
    ).get("healthy", True)
    return _result("ok" if ok else "fail", parsed.netloc)


def check_telegram(svc) -> dict:
    token = (getattr(config, "NOTIFY_TELEGRAM_TOKEN", "") or "").strip()
    if not token or not getattr(config, "NOTIFY_TELEGRAM_CHAT", ""):
        return _result("off", t("health_notify_off"))
    try:
        response = _http(f"https://api.telegram.org/bot{token}/getMe")
    except requests.RequestException:
        return _result("fail", t("health_unreachable", host="api.telegram.org"))
    if not response.ok:
        return _result("fail", t("health_key_rejected"))
    name = (response.json().get("result") or {}).get("username") or ""
    return _result("ok", f"@{name}" if name else t("health_ok"))


def check_disk(svc) -> dict:
    folder = os.path.join(config.BASE_DIR, "cache")
    usage = shutil.disk_usage(folder if os.path.isdir(folder) else config.BASE_DIR)
    free_gb = usage.free / 1024**3
    detail = t(
        "health_disk", free=f"{free_gb:.1f}".replace(".", "," if _czech() else ".")
    )
    return _result("warn" if usage.free < LOW_DISK else "ok", detail)


def check_app(svc) -> dict:
    hours = (time.time() - STARTED) / 3600
    uptime = (
        f"{hours / 24:.0f} d"
        if hours >= 48
        else f"{hours:.0f} h"
        if hours >= 1
        else "< 1 h"
    )
    commit = f" ({version.COMMIT})" if version.COMMIT else ""
    return _result(
        "ok", t("health_app", version=f"{version.VERSION}{commit}", uptime=uptime)
    )


def _czech() -> bool:
    from .i18n import current_language

    return current_language() == "cs"


# (id, group, name key or text, check)
CHECKS = (
    ("torrserver", "streaming", "TorrServer", check_torrserver),
    ("ffmpeg", "streaming", "ffmpeg", check_ffmpeg),
    ("tmdb", "catalogue", "TMDB", check_tmdb),
    ("imdb", "catalogue", "health_name_imdb", check_imdb),
    ("opensubtitles", "subtitles", "OpenSubtitles.com", check_opensubtitles),
    ("opensubtitles_org", "subtitles", "OpenSubtitles.org", check_opensubtitles_org),
    ("translation", "subtitles", "health_name_translation", check_translation),
    ("ai_subtitles", "ai", "health_name_ai", check_ai_subtitles),
    ("whisper", "ai", "health_name_whisper", check_whisper),
    ("speech_api", "ai", "health_name_speech_api", check_speech_api),
    ("ntfy", "notifications", "ntfy", check_ntfy),
    ("telegram", "notifications", "Telegram", check_telegram),
    ("disk", "system", "health_name_disk", check_disk),
    ("app", "system", "StreamFinder", check_app),
)


def _timed(svc, check) -> tuple[dict, int]:
    started = time.monotonic()
    try:
        result = check(svc)
    except Exception as e:  # a broken check is a failed service, never a broken page
        result = _result("fail", str(e)[:120])
    return result, round((time.monotonic() - started) * 1000)


def run_checks() -> list[dict]:
    svc = services()
    jobs = list(CHECKS)
    # Each check its own copy of the request: the texts come in the visitor's language.
    calls = [copy_current_request_context(partial(_timed, svc, job[3])) for job in jobs]
    with ThreadPoolExecutor(max_workers=8) as pool:
        indexes = pool.submit(svc.torrents.provider_status)
        done = list(pool.map(lambda call: call(), calls))
        try:
            providers = indexes.result(timeout=20)
        except Exception:  # noqa: BLE001 - shown as one failed row instead
            providers = []
    checks = []
    for (check_id, group, name, _), (result, ms) in zip(jobs, done, strict=True):
        label = t(name) if name.startswith("health_name_") else name
        checks.append(
            {"id": check_id, "group": group, "name": label, "ms": ms, **result}
        )
    for p in providers:
        checks.append(
            {
                "id": f"index_{p['name'].lower()}",
                "group": "catalogue",
                "name": p["name"],
                "ms": None,
                "state": "ok" if p["available"] else "fail",
                "detail": _host(p["url"]) if p["available"] else t("not_responding"),
            }
        )
    order = {group: i for i, group in enumerate(GROUPS)}
    return sorted(checks, key=lambda c: order[c["group"]])


@health.route("/api/health")
def health_checks():
    """Every check (?fresh=1: not the copy kept for a few seconds)."""
    from .i18n import current_language

    lang = current_language()
    with _lock:
        cached = _cache.get(lang)
        fresh = cached and time.monotonic() - cached[0] < KEEP_FOR
    if fresh and request.args.get("fresh") != "1":
        checks = cached[1]
    else:
        checks = run_checks()
        with _lock:
            _cache[lang] = (time.monotonic(), checks)
    return jsonify(
        success=True,
        checks=checks,
        groups={g: t(f"health_group_{g}") for g in GROUPS},
        summary={
            s: sum(c["state"] == s for c in checks)
            for s in ("ok", "warn", "fail", "off")
        },
        checked=time.time(),
    )
