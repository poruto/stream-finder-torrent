"""Server settings API (admin): API keys and logins, AI subtitles, TorrServer's
network use, and the admin account itself."""

import logging
import re

import requests
from flask import Blueprint, jsonify

from . import auth, config
from .api_helpers import json_body
from .auth import admin_required
from .i18n import t
from .services import services
from .subtitles.transcribe import LOCAL_URL
from .subtitles.whisper_models import MODELS as WHISPER_MODELS
from .tmdb import TMDBError
from .torrserver import READ_AHEAD

logger = logging.getLogger(__name__)
admin = Blueprint("admin", __name__, url_prefix="/api")


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


@admin.route("/keys")
@admin_required
def keys_status():
    """Which keys are configured. The keys themselves are never sent back."""
    return jsonify(success=True, **_key_status())


@admin.route("/keys", methods=["POST"])
@admin_required
def keys_update():
    """Body: any of {tmdb_api_key, opensubtitles_api_key, opensubtitles_username, opensubtitles_password,
    transcribe_api_key, torrserver_upload, torrserver_read_ahead} plus optional
    remove: ["opensubtitles", "transcribe"].
    Every new key is tested before it is saved."""
    body = json_body()
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


@admin.route("/admin/login", methods=["POST"])
def admin_login():
    body = json_body()
    ok, error = auth.login(
        str(body.get("username") or ""), str(body.get("password") or "")
    )
    if not ok:
        return jsonify(success=False, error=error), 401
    return jsonify(success=True, default_password=auth.using_default())


@admin.route("/admin/logout", methods=["POST"])
def admin_logout():
    auth.logout()
    return jsonify(success=True)


@admin.route("/admin/credentials", methods=["POST"])
@admin_required
def admin_credentials():
    """Body: {current_password, username?, new_password}"""
    body = json_body()
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
