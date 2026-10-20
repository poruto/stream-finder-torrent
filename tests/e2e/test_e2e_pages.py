"""Pages without the player: home, calendar, status, settings."""

import time

import pytest

from streamfinder import version

from . import fakes

pytest.importorskip("playwright.sync_api")


def test_home_renders_rows_with_visible_captions(page):
    page.open("/")
    rows = page.locator(".rows section.row[data-row]")
    assert rows.count() >= 3
    assert page.locator(".hero-billboard .hero-title").inner_text() == "Test Movie"

    poster = page.locator(
        f'.row[data-row="popular_movies"] .poster[data-id="{fakes.MOVIE_ID}"]'
    )
    caption = poster.locator(".poster-meta")
    # No hover: the title, year and rating are shown under every poster.
    page.mouse.move(0, 0)
    assert caption.is_visible()
    style = caption.evaluate(
        "n => { const s = getComputedStyle(n); return [s.opacity, s.visibility]; }"
    )
    assert float(style[0]) > 0.9 and style[1] == "visible"
    assert poster.locator(".poster-title").inner_text() == "Test Movie"
    assert "2020" in poster.locator(".poster-sub").inner_text()
    assert poster.locator(".poster-sub .rating").inner_text().strip() == "7.8"
    box = poster.locator(".poster-title").bounding_box()
    assert box and box["height"] > 5 and box["width"] > 20


def test_unreleased_title_has_out_badge(page):
    page.open("/")
    poster = page.locator(
        f'.row[data-row="popular_movies"] .poster[data-id="{fakes.FUTURE_ID}"]'
    )
    badge = poster.locator(".coming-badge")
    assert badge.is_visible()
    assert badge.inner_text() == "Out 18 Dec 2027"
    # Not out yet: no rating next to the year.
    assert poster.locator(".poster-sub .rating").count() == 0
    released = page.locator(
        f'.row[data-row="popular_movies"] .poster[data-id="{fakes.MOVIE_ID}"]'
    )
    assert released.locator(".coming-badge").count() == 0


@pytest.mark.parametrize(
    "path",
    [
        "/",
        f"/movie/{fakes.MOVIE_ID}",
        f"/show/{fakes.SHOW_ID}",
        "/movies",
        "/calendar",
    ],
)
@pytest.mark.console_errors_ok  # the visitor's 401 from /api/keys, see below
def test_without_tmdb_key_pages_lead_to_settings(page, monkeypatch, path):
    from streamfinder import config

    monkeypatch.setattr(config, "TMDB_API_KEY", "")
    page.open(path)
    assert page.url.endswith("/settings?setup=1"), page.url
    assert page.locator("#admin-panel").get_attribute("open") is not None


def test_status_works_without_tmdb_key(page, monkeypatch):
    from streamfinder import config

    monkeypatch.setattr(config, "TMDB_API_KEY", "")
    page.login_admin()
    page.open("/status")
    assert page.url.endswith("/status")


def test_calendar_groups_followed_shows_by_day(page):
    page.set_storage(
        {
            "streamfinder.progress.v1": {
                f"tv:{fakes.UPCOMING_ID}": {
                    "title": "Upcoming Show",
                    "url": f"/show/{fakes.UPCOMING_ID}",
                    "season": 1,
                    "episode": 2,
                    "updated": int(time.time() * 1000),
                }
            }
        }
    )
    page.open("/calendar")
    days = page.locator("#calendar .calendar-day")
    days.first.wait_for()
    assert days.count() == 2
    first = days.nth(0)
    assert first.locator("h2").inner_text() == "Tomorrow"
    # Two episodes out the same day are one row.
    items = first.locator(".calendar-item")
    assert items.count() == 1
    assert "Upcoming Show" in items.nth(0).inner_text()
    assert "Season 2, episodes 1-2" in items.nth(0).inner_text()
    assert items.nth(0).locator(".calendar-tag").inner_text() == "Season premiere"
    assert items.nth(0).get_attribute("href") == f"/show/{fakes.UPCOMING_ID}"
    assert days.nth(1).locator(".calendar-item").count() == 1
    assert page.locator("#calendar-status").is_hidden()


def test_calendar_without_followed_titles(page):
    page.open("/calendar")
    page.wait_for_function(
        "document.getElementById('calendar-status').textContent.trim() !== ''"
    )
    assert page.locator("#calendar .calendar-day").count() == 0
    assert page.locator("#calendar-status").is_visible()


def test_status_page_lists_services_and_storage(page, torrserver):
    torrserver.added[fakes.MOVIE_HASH] = {"title": "Test Movie", "poster": ""}
    page.login_admin()
    page.open("/status")
    first = page.locator(".status-list li").first
    assert first.get_attribute("data-ok") == "yes"  # TorrServer answers /echo
    assert "FakeIndex" in page.locator(".status-list").inner_text()
    page.wait_for_function(
        "document.querySelectorAll('#library-list li').length > 0", timeout=10_000
    )
    assert "Test Movie" in page.locator("#library-list").inner_text()


def test_backup_downloads_and_restores(page, tmp_path, monkeypatch):
    """The Status page's backup: a file to keep, and the same file merged back."""
    from streamfinder import config, notify, shared

    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "settings.env"))
    monkeypatch.setattr(notify, "STATE", str(tmp_path / "notified.json"))
    shared.write_json(
        shared.PATH,
        {"progress": {"movie:1": {"position": 60, "updated": 100}}, "list": {}},
    )
    page.login_admin()
    page.open("/status")
    page.check("#backup-keys")
    assert page.get_attribute("#backup-download", "href") == "/api/backup?keys=1"
    page.uncheck("#backup-keys")
    with page.expect_download() as info:
        page.click("#backup-download")
    backup = tmp_path / info.value.suggested_filename
    info.value.save_as(backup)
    assert backup.name.startswith("streamfinder-backup-")

    shared.write_json(shared.PATH, {})  # everything lost
    page.once("dialog", lambda dialog: dialog.accept())
    page.set_input_files("#backup-file", str(backup))  # a fetch with the page's Origin
    page.wait_for_function(
        "document.getElementById('backup-message').textContent !== ''",
        timeout=10_000,
    )
    assert page.inner_text("#backup-message").startswith("Restored – profiles: 1,")
    assert shared._read()["progress"]["movie:1"]["position"] == 60
    assert not page.errors, "\n".join(page.errors)


def test_settings_fit_a_phone_screen(phone):
    phone.login_admin()
    phone.open("/settings")
    assert phone.locator("#settings-form").is_visible()
    width = phone.evaluate("[document.scrollingElement.scrollWidth, innerWidth]")
    assert width[0] <= width[1], f"page is {width[0]}px wide on a {width[1]}px screen"


@pytest.mark.parametrize(
    "path", ["/", "/movies", "/shows", "/search?q=test", "/calendar"]
)
def test_pages_fit_a_phone_screen(phone, path):
    phone.open(path)
    width = phone.evaluate("[document.scrollingElement.scrollWidth, innerWidth]")
    assert width[0] <= width[1], f"{path} is {width[0]}px wide on a {width[1]}px screen"


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/movies",
        "/shows",
        "/search?q=test",
        f"/movie/{fakes.MOVIE_ID}",
        f"/movie/{fakes.FUTURE_ID}",
        f"/show/{fakes.SHOW_ID}",
        "/calendar",
        "/status",
        "/settings",
    ],
)
def test_no_console_errors_for_admin(page, path):
    page.login_admin()
    page.open(path)
    page.wait_for_load_state("networkidle")
    assert not page.errors, "\n".join(page.errors)


@pytest.mark.parametrize(
    "path, endpoint",
    [("/settings", "/api/keys"), ("/status", "/api/library")],
)
def test_no_console_errors_for_visitors(page, path, endpoint):
    """Someone who isn't the admin opens a page that has admin parts."""
    page.open(path)
    page.wait_for_load_state("networkidle")
    assert not page.errors, "\n".join(page.errors)


CACHED_URLS = """async () => {
    const cache = await caches.open("streamfinder-" + VERSION);
    return (await cache.keys()).map((request) => request.url);
}"""


def test_service_worker_keeps_the_app_shell_and_the_pages_still_work(page):
    page.open("/")
    # localhost is a secure context: the worker registers and takes the pages from the root.
    scope = page.evaluate("navigator.serviceWorker.ready.then((r) => r.scope)")
    assert scope == page.base_url + "/"
    page.wait_for_function("navigator.serviceWorker.controller !== null")
    assert page.evaluate("caches.keys()") == [f"streamfinder-{version.VERSION}"]
    shell = page.evaluate(CACHED_URLS.replace("VERSION", repr(version.VERSION)))
    assert any("/static/css/app.css?v=" in url for url in shell)
    assert any("/static/js/player/core.js?v=" in url for url in shell)

    # With the worker in charge: the home page and a film work as before.
    page.reload()
    page.wait_for_load_state("load")
    assert page.evaluate("navigator.serviceWorker.controller !== null")
    assert page.locator(".rows section.row[data-row]").count() >= 3
    page.open(f"/movie/{fakes.MOVIE_ID}")
    page.wait_for_function("!document.getElementById('play-btn').disabled")
    page.locator("#versions-btn").click()
    page.locator("#versions-picks .pick").first.wait_for()
    urls = page.evaluate(CACHED_URLS.replace("VERSION", repr(version.VERSION)))
    # The page is kept for offline use; nothing from the API ever is.
    assert f"{page.base_url}/movie/{fakes.MOVIE_ID}" in urls
    assert not [url for url in urls if "/api/" in url], urls


def _poster_size(page, selector):
    """The TMDB size (w185/w342/w500) the browser picked from the poster's srcset."""
    image = page.locator(selector).first
    image.scroll_into_view_if_needed()
    page.wait_for_function("img => img.currentSrc", arg=image.element_handle())
    return image.evaluate("img => img.currentSrc.split('/t/p/')[1].split('/')[0]")


@pytest.mark.parametrize("width, size", [(400, "w185"), (1280, "w342")])
def test_posters_come_in_the_size_that_fits(page, width, size):
    page.set_viewport_size({"width": width, "height": 800})
    page.set_storage(
        {
            "streamfinder.mylist.v1": [
                {
                    "key": f"movie:{fakes.MOVIE_ID}",
                    "id": fakes.MOVIE_ID,
                    "type": "movie",
                    "title": "Test Movie",
                    "poster": f"https://image.tmdb.org/t/p/w500/poster{fakes.MOVIE_ID}.jpg",
                    "url": f"/movie/{fakes.MOVIE_ID}",
                    "added": 1,
                }
            ]
        }
    )
    page.open("/")
    # Rendered on the server (a row) and by app.js (My list, from a stored w500 URL).
    assert _poster_size(page, '.row[data-row="popular_movies"] .poster img') == size
    assert _poster_size(page, "#mylist-track .poster img") == size
