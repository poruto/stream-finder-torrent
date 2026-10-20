"""StreamFinder: browse TMDB, pick the best torrent automatically and stream it
through TorrServer with automatically matched subtitles."""

import json
import logging
import os

from flask import Flask, jsonify, render_template, request, url_for

from . import compression, config, csrf, i18n, version
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
        from . import notify

        notify.start(app)  # new-episode notifications (when a channel is set)
    app.extensions["streamfinder"] = services

    from .admin import admin
    from .api import api
    from .library import library
    from .playback import playback
    from .shared import shared
    from .subtitle_api import subtitle_api
    from .views import web

    app.register_blueprint(web)
    app.register_blueprint(api)
    app.register_blueprint(playback)
    app.register_blueprint(subtitle_api)
    app.register_blueprint(admin)
    app.register_blueprint(shared)
    app.register_blueprint(library)
    from .notify import notify as notify_bp
    from .upcoming import upcoming_bp

    app.register_blueprint(upcoming_bp)
    app.register_blueprint(notify_bp)
    from .health import health

    app.register_blueprint(health)

    # Globals (not a context processor) so that imported macros can translate too.
    app.jinja_env.globals.update(
        t=i18n.t,
        plural=i18n.plural,
        js_strings=i18n.js_strings,
        js_strings_json=i18n.js_strings_json,
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

    @app.get("/sw.js")
    def service_worker():
        """The offline cache (static/sw.js) from the root, so every page is in its scope.

        Filled in here: the app version (a cache of its own, so a release replaces the old
        one) and the app shell's URLs exactly as the pages ask for them.
        """
        static = app.static_folder
        shell = [url_for("static", filename="manifest.json")]
        # CSS and scripts carry ?v=<mtime> (asset()); the icons are linked without it.
        for folder, versioned in (("css", True), ("js", True), ("icons", False)):
            for root, _, files in sorted(os.walk(os.path.join(static, folder))):
                for name in sorted(files):
                    path = os.path.relpath(os.path.join(root, name), static)
                    path = path.replace(os.sep, "/")
                    shell.append(
                        asset(path) if versioned else url_for("static", filename=path)
                    )
        with open(os.path.join(static, "sw.js"), encoding="utf-8") as f:
            script = f.read()
        script = script.replace(
            'const VERSION = "dev";',
            f"const VERSION = {json.dumps(version.VERSION)};",
            1,
        )
        script = script.replace(
            "const SHELL = [];", f"const SHELL = {json.dumps(shell)};", 1
        )
        response = app.response_class(script, mimetype="text/javascript")
        response.headers["Service-Worker-Allowed"] = "/"
        response.cache_control.no_cache = True
        return response

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

    csrf.init_app(app)  # state-changing requests only from the app's own pages
    compression.init_app(app)

    if not testing and (
        not config.DEBUG or os.environ.get("WERKZEUG_RUN_MAIN") == "true"
    ):
        services.tmdb.persist(config.TMDB_CACHE_FILE)

    return app
