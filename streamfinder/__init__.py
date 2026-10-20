"""StreamFinder: browse TMDB, pick the best torrent automatically and stream it
through TorrServer with automatically matched subtitles."""

import logging
import os

from flask import Flask, jsonify, render_template, request, url_for

from . import config, i18n, version
from .services import Services
from .tmdb import TMDBError


def create_app(testing: bool = False) -> Flask:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.SECRET_KEY
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=30 * 86400,
    )
    app.json.sort_keys = False
    services = Services.create()
    # With the debug reloader only the child process (WERKZEUG_RUN_MAIN) serves requests.
    if not testing and (
        not config.DEBUG or os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    ):
        services.imdb.start()
        services.apply_network_later()  # upload limit / read-ahead from Server settings
    app.extensions["streamfinder"] = services

    from .api import api
    from .library import library
    from .shared import shared
    from .views import web

    app.register_blueprint(web)
    app.register_blueprint(api)
    app.register_blueprint(shared)
    app.register_blueprint(library)
    from .upcoming import upcoming_bp

    app.register_blueprint(upcoming_bp)

    # Globals (not a context processor) so that imported macros can translate too.
    app.jinja_env.globals.update(
        t=i18n.t,
        plural=i18n.plural,
        js_strings=i18n.js_strings,
        ui_languages=i18n.LANGUAGES,
        subtitle_defaults=config.SUBTITLE_LANGUAGES,
        app_version=version.VERSION,
        app_commit=version.COMMIT,
        repo_url=version.REPO_URL,
    )

    @app.context_processor
    def inject_language():
        return {"lang": i18n.current_language()}

    @app.template_global()
    def asset(filename: str) -> str:
        """Static URL with the file's mtime appended, so browsers never run stale CSS/JS."""
        path = os.path.join(app.static_folder, filename)
        version = int(os.path.getmtime(path)) if os.path.exists(path) else 0
        return url_for("static", filename=filename, v=version)

    @app.errorhandler(TMDBError)
    def tmdb_unavailable(error):
        logging.getLogger(__name__).warning("%s", error)
        if request.path.startswith("/api/"):
            return jsonify(success=False, error=i18n.t("error_tmdb")), 502
        return render_template("error.html", message=i18n.t("error_tmdb")), 502

    @app.errorhandler(404)
    def not_found(error):
        if request.path.startswith("/api/"):
            return jsonify(success=False, error=i18n.t("err_not_found")), 404
        return render_template("error.html", message=i18n.t("error_not_found")), 404

    @app.after_request
    def cache_static(response):
        if request.path.startswith("/static/"):
            response.cache_control.public = True
            response.cache_control.max_age = 31536000 if request.args.get("v") else 300
        return response

    return app
