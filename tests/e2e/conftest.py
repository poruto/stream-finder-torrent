"""Headless browser tests: the real app in a background thread, a fake TMDB, TorrServer
and torrent index, and Chromium driven by Playwright.

Skipped as a whole when Playwright or its Chromium isn't installed
(pip install -r requirements-dev.txt && python -m playwright install chromium).
"""

import base64
import os
import threading

import pytest

from . import fakes

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ImportError:  # dev requirements not installed: every test here is skipped
    sync_playwright = None

DESKTOP = {"width": 1280, "height": 800}
SUBTITLE_LANGUAGES = ["cs", "en"]
GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


# -- the world outside the app --------------------------------------------------------


@pytest.fixture(scope="session")
def media(tmp_path_factory):
    """Test videos and subtitles, made once per run with the ffmpeg from imageio-ffmpeg."""
    ffmpeg = fakes.ffmpeg_exe()
    if not ffmpeg:
        pytest.skip("imageio-ffmpeg is not installed (needed to make the test videos)")
    root = tmp_path_factory.mktemp("media")
    files = {
        "single": str(root / "single.mp4"),
        "dual": str(root / "dual.mp4"),
        "cs.srt": str(root / "cs.srt"),
        "en.srt": str(root / "en.srt"),
    }
    fakes.make_video(ffmpeg, files["single"], ["eng"])
    fakes.make_video(ffmpeg, files["dual"], ["cze", "eng"])
    for language in ("cs", "en"):
        with open(files[f"{language}.srt"], "w", encoding="utf-8") as f:
            f.write(fakes.srt(language))
    return files


@pytest.fixture(scope="session")
def torrserver(media):
    episodes = [
        (n, f"Test.Show.S01/Test.Show.S01E0{n}.1080p.WEB-DL.x264.mp4", media["dual"])
        for n in (1, 2, 3)
    ]
    server = fakes.FakeTorrServer(
        {
            fakes.MOVIE_HASH: {
                "title": fakes.MOVIE_NAME,
                "files": [
                    (1, f"{fakes.MOVIE_NAME}/{fakes.MOVIE_NAME}.mp4", media["single"]),
                    (
                        2,
                        f"{fakes.MOVIE_NAME}/{fakes.MOVIE_NAME}.cs.srt",
                        media["cs.srt"],
                    ),
                    (
                        3,
                        f"{fakes.MOVIE_NAME}/{fakes.MOVIE_NAME}.en.srt",
                        media["en.srt"],
                    ),
                ],
            },
            fakes.SHOW_HASH: {"title": fakes.SHOW_NAME, "files": episodes},
        }
    ).start()
    yield server
    server.stop()


@pytest.fixture(scope="session")
def app_server(torrserver, tmp_path_factory):
    """The app on a free port, its services pointed at the fakes."""
    from werkzeug.serving import make_server

    from streamfinder import config, create_app

    cache = tmp_path_factory.mktemp("cache")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "TMDB_API_KEY", "test")
        mp.setattr(config, "TORRSERVER_URL", torrserver.url)
        mp.setattr(config, "TORRSERVER_PUBLIC_URL", "")
        mp.setattr(config, "SUBTITLE_CACHE_DIR", str(cache / "subtitles"))
        mp.setattr(config, "ADMIN_USERNAME", "")
        mp.setattr(config, "ADMIN_PASSWORD_HASH", "")  # admin / admin
        app = create_app(testing=True)
        app.jinja_env.globals["subtitle_defaults"] = SUBTITLE_LANGUAGES
        svc = app.extensions["streamfinder"]
        svc.tmdb = fakes.FakeTMDB(fakes.tmdb_data())
        svc.torrserver.base_url = torrserver.url
        svc.torrserver.stream_path = "/stream"
        svc.torrents.sktorrent.configure("", "")
        svc.torrents.providers = [fakes.FakeProvider()]
        svc.subtitles.api.configure(
            "", "", ""
        )  # no OpenSubtitles: only the torrent's own
        svc.subtitles.org.search = lambda *args, **kwargs: []
        svc.subtitles.languages = SUBTITLE_LANGUAGES
        svc.subtitles.cache_dir = str(cache / "subtitles")
        os.makedirs(svc.subtitles.cache_dir, exist_ok=True)
        svc.transcriber.configure("", "", "", False)  # no AI subtitles

        server = make_server("127.0.0.1", 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield {
                "url": f"http://127.0.0.1:{server.server_port}",
                "app": app,
                "services": svc,
            }
        finally:
            server.shutdown()
            server.server_close()


@pytest.fixture
def base_url(app_server, torrserver, tmp_path, monkeypatch):
    """The app's URL; every test starts with an empty household (shared progress) and TorrServer."""
    from streamfinder import shared

    monkeypatch.setattr(shared, "PATH", str(tmp_path / "shared.json"))
    torrserver.reset()
    return app_server["url"]


# -- the browser ----------------------------------------------------------------------


@pytest.fixture(scope="session")
def playwright_instance():
    if sync_playwright is None:
        pytest.skip("playwright is not installed (pip install -r requirements-dev.txt)")
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(playwright_instance):
    try:
        browser = playwright_instance.chromium.launch(
            # E2E_BROWSER_CHANNEL=chrome (CI): Google Chrome, which plays H.264/AAC on
            # Linux too; Playwright's own Chromium skips the playback tests there.
            channel=os.getenv("E2E_BROWSER_CHANNEL") or None,
            args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"],
        )
    except PlaywrightError as e:
        pytest.skip(
            f"Chromium is not installed (python -m playwright install chromium): {e}"
        )
    yield browser
    browser.close()


@pytest.fixture(scope="session")
def h264(browser):
    """Whether this Chromium build plays H.264/AAC (the test videos)."""
    page = browser.new_page()
    try:
        return bool(
            page.evaluate(
                """() => document.createElement("video")
                    .canPlayType('video/mp4; codecs="avc1.42E01E, mp4a.40.2"')"""
            )
        )
    finally:
        page.close()


def _offline(route):
    """Nothing leaves the machine: TMDB images, Google Fonts and the like get stubs."""
    url = route.request.url
    if route.request.resource_type == "image" or url.endswith((".jpg", ".png")):
        return route.fulfill(status=200, content_type="image/gif", body=GIF)
    if "fonts.googleapis.com" in url:
        return route.fulfill(status=200, content_type="text/css", body="")
    return route.fulfill(status=200, content_type="text/plain", body="")


class Page:
    """A Playwright page plus the console errors it collected."""

    def __init__(self, page, base_url):
        self.page = page
        self.base_url = base_url
        self.errors: list[str] = []
        page.on(
            "console",
            lambda msg: (
                msg.type == "error"
                and self.errors.append(
                    f"console: {msg.text} ({msg.location.get('url')})"
                )
            ),
        )
        page.on("pageerror", lambda error: self.errors.append(f"pageerror: {error}"))

    def __getattr__(self, name):
        return getattr(self.page, name)

    def open(self, path: str, **kwargs):
        response = self.page.goto(self.base_url + path, **kwargs)
        self.page.wait_for_load_state("load")
        return response

    def set_storage(self, values: dict):
        """Write localStorage on the app's origin (a blank page of it) before the real visit."""
        if not self.page.url.startswith(self.base_url):
            self.page.goto(self.base_url + "/api/healthz")
        self.page.evaluate(
            """values => { for (const [k, v] of Object.entries(values)) localStorage.setItem(k, JSON.stringify(v)); }""",
            values,
        )

    def storage(self, key: str):
        return self.page.evaluate("key => JSON.parse(localStorage.getItem(key))", key)

    def login_admin(self):
        response = self.page.request.post(
            self.base_url + "/api/admin/login",
            data={"username": "admin", "password": "admin"},
        )
        assert response.ok, response.text()


def _new_page(browser, base_url, request, **options):
    context = browser.new_context(locale="en-US", **options)
    context.add_cookies([{"name": "lang", "value": "en", "url": base_url}])
    context.route(
        lambda url: (
            not url.startswith(base_url) and not url.startswith("http://127.0.0.1")
        ),
        _offline,
    )
    context.set_default_timeout(10_000)
    page = Page(context.new_page(), base_url)
    return context, page


def _check_console(request, page):
    """Any console error or uncaught exception fails the test (unless it is an expected bug)."""
    if request.node.get_closest_marker("xfail") or request.node.get_closest_marker(
        "console_errors_ok"
    ):
        return
    report = getattr(request.node, "rep_call", None)
    if report is not None and report.failed:
        return  # the test failed already; its own message matters more
    assert not page.errors, "errors in the browser console:\n" + "\n".join(page.errors)


@pytest.fixture
def page(browser, base_url, request):
    context, page = _new_page(browser, base_url, request, viewport=DESKTOP)
    yield page
    try:
        _check_console(request, page)
    finally:
        context.close()


@pytest.fixture
def phone(browser, playwright_instance, base_url, request):
    device = dict(playwright_instance.devices["Pixel 7"])
    device.pop("default_browser_type", None)
    context, page = _new_page(browser, base_url, request, **device)
    yield page
    try:
        _check_console(request, page)
    finally:
        context.close()


@pytest.fixture
def media_page(page, h264):
    """A desktop page for tests that play video."""
    if not h264:
        pytest.skip("this Chromium build can't play H.264/AAC")
    return page


@pytest.fixture
def media_phone(phone, h264):
    """A phone for tests that play video."""
    if not h264:
        pytest.skip("this Chromium build can't play H.264/AAC")
    return phone


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    report = yield
    setattr(item, f"rep_{report.when}", report)
    return report


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "console_errors_ok: the test expects errors in the browser console"
    )
