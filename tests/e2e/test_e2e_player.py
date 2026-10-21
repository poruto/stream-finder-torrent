"""The player: opening, Back, subtitles, audio tracks and the keyboard."""

import pytest

from . import fakes

pytest.importorskip("playwright.sync_api")

PLAYING = """() => { const v = document.getElementById("video");
    return Boolean(v.getAttribute("src")) && !v.paused && v.currentTime > 0.3 && v.readyState >= 3; }"""
# Where the film is (a converted stream's clock starts at its ?start=).
POSITION = """() => { const v = document.getElementById("video");
    const m = /[?&]start=([\\d.]+)/.exec(v.getAttribute("src") || "");
    return (m ? Number(m[1]) : 0) + v.currentTime; }"""
TRACKS = """() => { const v = document.getElementById("video");
    const showing = [...v.textTracks].filter(t => t.mode === "showing");
    const active = showing.flatMap(t => [...(t.activeCues || [])].map(c => c.text));
    return { elements: v.querySelectorAll("track").length, showing: showing.length,
             cues: showing.map(t => t.cues ? t.cues.length : 0), active }; }"""


def wait_playing(page, timeout=20_000):
    page.wait_for_function(PLAYING, timeout=timeout)


def start_movie(page, settings=None):
    if settings:
        page.set_storage({"streamfinder.settings.v1": settings})
    page.open(f"/movie/{fakes.MOVIE_ID}")
    page.wait_for_function("!document.getElementById('play-btn').disabled")
    page.locator("#play-btn").click()
    page.locator("#player").wait_for(state="visible")
    wait_playing(page)


def start_show(page):
    page.open(f"/show/{fakes.SHOW_ID}")
    page.wait_for_function("!document.getElementById('play-btn').disabled")
    page.locator("#play-btn").click()
    wait_playing(page)
    page.locator("#audio-btn").wait_for(state="visible")


# -- opening and closing ------------------------------------------------------------


def test_play_opens_the_player_and_back_closes_it(media_page, torrserver):
    page = media_page
    start_movie(page)
    src = page.locator("#video").get_attribute("src")
    assert src.startswith(torrserver.url + "/stream/") and fakes.MOVIE_HASH in src
    start = page.evaluate("document.getElementById('video').currentTime")
    page.wait_for_function(
        f"document.getElementById('video').currentTime > {start + 0.5}"
    )

    assert page.evaluate("history.state && history.state.player") is True
    # Everything behind the player is inert: no focus, no clicks there.
    assert page.evaluate("document.querySelector('main').inert") is True
    assert page.evaluate("document.getElementById('nav').inert") is True
    assert page.evaluate("document.getElementById('player').inert") is False

    page.go_back()
    page.locator("#player").wait_for(state="hidden")
    assert page.url.endswith(f"/movie/{fakes.MOVIE_ID}")
    assert page.evaluate("document.getElementById('video').paused") is True
    assert page.evaluate("document.getElementById('video').getAttribute('src')") is None
    assert page.evaluate("document.querySelector('main').inert") is False
    assert (
        page.evaluate("document.activeElement && document.activeElement.id")
        == "play-btn"
    )
    for _ in range(30):  # the stop request goes out in the background
        if fakes.MOVIE_HASH in torrserver.dropped:
            break
        page.wait_for_timeout(100)
    assert fakes.MOVIE_HASH in torrserver.dropped

    # The page still works: playing again starts again, Back closes again.
    page.locator("#play-btn").click()
    wait_playing(page)
    assert page.evaluate("history.state && history.state.player") is True
    page.go_back()
    page.locator("#player").wait_for(state="hidden")
    assert page.url.endswith(f"/movie/{fakes.MOVIE_ID}")


def test_close_button_leaves_no_player_history_entry(media_page):
    page = media_page
    start_movie(page)
    page.locator("#player-close").click()
    page.locator("#player").wait_for(state="hidden")
    page.wait_for_function("!(history.state && history.state.player)")
    assert page.url.endswith(f"/movie/{fakes.MOVIE_ID}")
    assert (
        page.evaluate("document.activeElement && document.activeElement.id")
        == "play-btn"
    )


def test_versions_open_in_full_screen_without_leaving_it(media_page):
    page = media_page
    start_movie(page)
    page.locator("#ctl-fullscreen").click()
    page.wait_for_function("document.fullscreenElement?.id === 'player'")
    page.locator("#player-versions").click()
    page.locator("#versions .sheet-panel").wait_for(state="visible")
    # Only the full-screen element is drawn: the sheet opened inside it.
    assert page.evaluate("document.fullscreenElement?.id") == "player"
    assert page.evaluate("document.getElementById('versions').parentElement.id") == (
        "player"
    )
    assert page.evaluate("document.getElementById('versions').inert") is False
    assert page.evaluate("document.querySelector('.player-controls').inert") is True
    page.locator("#versions .sheet-head [data-close]").click()
    page.locator("#versions").wait_for(state="hidden")
    assert page.evaluate("document.fullscreenElement?.id") == "player"
    assert page.evaluate("document.querySelector('.player-controls').inert") is False

    # Out of full screen and the player, the page's button opens it where it was.
    page.evaluate("document.exitFullscreen()")
    page.wait_for_function("!document.fullscreenElement")
    page.locator("#player-close").click()
    page.locator("#player").wait_for(state="hidden")
    page.locator("#versions-btn").click()
    page.locator("#versions .sheet-panel").wait_for(state="visible")
    assert page.evaluate("document.getElementById('versions').parentElement.id") != (
        "player"
    )
    assert page.evaluate("document.querySelector('main').inert") is True


def test_player_fits_a_phone(media_phone):
    page = media_phone
    page.open(f"/movie/{fakes.MOVIE_ID}")
    page.wait_for_function("!document.getElementById('play-btn').disabled")
    page.locator("#play-btn").tap()
    wait_playing(page)
    width = page.evaluate("[document.scrollingElement.scrollWidth, innerWidth]")
    assert width[0] <= width[1]
    box = page.locator("#player-controls").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= width[1] + 1


# -- subtitles ------------------------------------------------------------------------


def wait_subtitle(page, language):
    page.wait_for_function(
        """language => { const v = document.getElementById("video");
            const t = [...v.textTracks].filter(t => t.mode === "showing");
            return t.length === 1 && t[0].cues && t[0].cues.length > 0
                && t[0].cues[0].text.startsWith(language); }""",
        arg=language,
        timeout=15_000,
    )


@pytest.mark.parametrize(
    "settings", [None, {"alwaysConvert": True}], ids=["direct", "converted"]
)
def test_subtitles_load_switch_and_follow_a_seek(media_page, settings):
    page = media_page
    start_movie(page, settings)
    wait_subtitle(page, "cs")  # the first subtitle language, from the torrent itself
    if settings:
        assert "/api/transcode/" in page.locator("#video").get_attribute("src")

    # Two quick picks: only the last one may end up showing.
    page.wait_for_function("!document.getElementById('subs-btn').disabled")
    page.locator("#subs-btn").click()
    page.locator("#subs-menu").wait_for(state="visible")
    page.locator("#subs-options .lang-chip", has_text="English").click()
    page.evaluate(
        """() => {
            const options = [...document.querySelectorAll("#subs-options .subs-option")];
            const english = options.find(o => o.querySelector("strong").textContent.endsWith(" en"));
            const czech = options.find(o => o.querySelector("strong").textContent === "Czech");
            english.click();
            czech.click();
        }"""
    )
    wait_subtitle(page, "cs")
    page.wait_for_timeout(500)  # a late English download must not add a second track
    tracks = page.evaluate(TRACKS)
    assert tracks["showing"] == 1 and tracks["elements"] == 1, tracks

    page.keyboard.press("Escape")
    page.locator("#subs-menu").wait_for(state="hidden")
    before = page.evaluate(POSITION)
    page.keyboard.press("ArrowRight")
    page.wait_for_function(f"({POSITION})() > {before + 7}", timeout=15_000)
    wait_playing(page)
    if settings:  # outside what was loaded: ffmpeg restarted at a keyframe there
        assert "start=0&" not in page.locator("#video").get_attribute("src")
    # Lines are one second apart: after the seek one shows, the one for this second.
    for _ in range(6):
        tracks = page.evaluate(TRACKS)
        position = page.evaluate(POSITION)
        assert len(tracks["active"]) <= 2, tracks
        for text in tracks["active"]:
            assert abs(int(text.rsplit(" ", 1)[1]) - position) <= 1.5, (text, position)
        page.wait_for_timeout(250)
    assert tracks["showing"] == 1


# -- audio tracks ---------------------------------------------------------------------


def audio_menu_columns(page):
    page.locator("#audio-btn").click()
    page.locator("#audio-menu").wait_for(state="visible")
    return page.evaluate(
        """() => [...document.querySelectorAll("#audio-options .subs-option")].map(o => {
            const box = n => { const r = n.getBoundingClientRect(); return [r.left, r.right, r.width]; };
            return { check: box(o.querySelector(".icon")), flag: box(o.querySelector(".flag")),
                     text: box(o.querySelector(".subs-text")), label: o.querySelector("strong").textContent,
                     checked: o.getAttribute("aria-checked") };
        })"""
    )


def assert_columns(options):
    assert len(options) == 2
    for option in options:
        check, flag, text = option["check"], option["flag"], option["text"]
        assert check[1] <= flag[0] + 1 and flag[1] <= text[0] + 1, (
            option
        )  # side by side
        assert text[2] > 100, f"audio label squeezed to {text[2]:.0f}px: {option}"


def test_audio_menu_and_choice_kept_for_next_episode(media_page):
    page = media_page
    start_show(page)
    options = audio_menu_columns(page)
    assert_columns(options)
    assert [o["label"] for o in options] == ["Czech", "English"]
    assert [o["checked"] for o in options] == ["true", "false"]

    page.locator("#audio-options .subs-option").nth(1).click()
    page.wait_for_function(
        "/\\/api\\/transcode\\/.*audio=1/.test(document.getElementById('video').getAttribute('src'))"
    )
    wait_playing(page)
    assert page.locator("#audio-label").inner_text() == "English"
    prefs = page.storage("streamfinder.progress.v1")[f"tv:{fakes.SHOW_ID}"]["prefs"]
    assert prefs["audio"] == "eng"

    # The next episode starts with the same track.
    page.locator("#next-btn").wait_for(state="visible")
    page.locator("#next-btn").click()
    page.wait_for_function(
        "document.getElementById('player-subtitle').textContent.includes('2')"
    )
    page.wait_for_function(
        """() => { const s = document.getElementById('video').getAttribute('src') || '';
            return s.includes('/api/transcode/') && /\\/2\\.mp4/.test(s) && s.includes('audio=1'); }"""
    )
    wait_playing(page)
    assert page.locator("#audio-label").inner_text() == "English"
    options = audio_menu_columns(page)
    assert [o["checked"] for o in options] == ["false", "true"]


def test_audio_menu_columns_on_a_phone(media_phone):
    page = media_phone
    page.open(f"/show/{fakes.SHOW_ID}")
    page.wait_for_function("!document.getElementById('play-btn').disabled")
    page.locator("#play-btn").tap()
    wait_playing(page)
    page.locator("#audio-btn").wait_for(state="visible")
    assert_columns(audio_menu_columns(page))


# -- keyboard -------------------------------------------------------------------------


def test_keyboard_controls(media_page):
    page = media_page
    start_movie(page)
    assert page.evaluate("document.activeElement.id") == "player"
    video = "document.getElementById('video')"

    page.keyboard.press("Space")
    page.wait_for_function(f"{video}.paused")
    page.keyboard.press("Space")
    page.wait_for_function(f"!{video}.paused")

    before = page.evaluate(f"{video}.currentTime")
    page.keyboard.press("ArrowRight")
    page.wait_for_function(f"{video}.currentTime > {before + 8}")
    after = page.evaluate(f"{video}.currentTime")
    page.keyboard.press("ArrowLeft")
    page.wait_for_function(f"{video}.currentTime < {after - 8}")

    # Escape closes the open menu first, then the player.
    page.wait_for_function("!document.getElementById('subs-btn').disabled")
    page.locator("#subs-btn").click()
    page.locator("#subs-menu").wait_for(state="visible")
    page.keyboard.press("Escape")
    page.locator("#subs-menu").wait_for(state="hidden")
    assert page.locator("#player").is_visible()
    page.keyboard.press("Escape")
    page.locator("#player").wait_for(state="hidden")
    assert page.url.endswith(f"/movie/{fakes.MOVIE_ID}")
