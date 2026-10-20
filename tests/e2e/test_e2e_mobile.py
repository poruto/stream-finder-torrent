"""Phones and tablets: every page and the player measured at real phone sizes.

Every page is opened at six screens (a notch where phones have one, emulated through
Chromium's safe-area override) in Czech - the longer labels - and the narrowest phone
and the landscape one also in English. The browser then measures it (see AUDIT):

- overflow:  the page scrolls sideways
- wide:      an element sticks out of the screen (rows that scroll sideways are fine)
- targets:   buttons, links and inputs smaller than 40 x 40 CSS px
- tiny:      text under 11 px
- inputs:    inputs and selects under 16 px (iOS zooms the page into them)
- clipped:   a control's label cut off, spilling out of it or wrapping where it must not
- covered:   a control hidden under another one (or under the fixed header / tab bar)
- tabbar:    the end of the page behind the tab bar, the tab bar under the home indicator
- safe:      something under the notch, the status bar or the home indicator

Contexts are made once per screen and language and reused, so the whole file stays fast.
"""

import json
import time

import pytest

from . import fakes
from .conftest import Page, _offline
from .test_e2e_player import wait_playing

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import TimeoutError as PlaywrightTimeout  # noqa: E402

GESTURE_MISSING = (AssertionError, PlaywrightTimeout)

NOTCH_PORTRAIT = {"top": 47, "right": 0, "bottom": 34, "left": 0}
NOTCH_LANDSCAPE = {"top": 0, "right": 47, "bottom": 21, "left": 47}
PIXEL_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Mobile Safari/537.36"
)
TABLET_UA = (
    "Mozilla/5.0 (Linux; Android 14; SM-X710) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
SCREENS = {
    "320x568": ((320, 568), 2, None),  # iPhone SE (1st gen), small Androids
    "360x800": ((360, 800), 3, None),  # the most common Android size
    "390x844": ((390, 844), 3, NOTCH_PORTRAIT),  # iPhone 12-16
    "412x915": ((412, 915), 2.625, None),  # Pixel
    "844x390": ((844, 390), 3, NOTCH_LANDSCAPE),  # an iPhone held sideways
    "768x1024": ((768, 1024), 2, None),  # tablet
}
# Czech everywhere (the longest labels), English on the narrowest and the sideways phone.
SCREEN_LANGS = [(s, "cs") for s in SCREENS] + [("320x568", "en"), ("844x390", "en")]
SCREEN_IDS = [f"{lang}-{screen}" for screen, lang in SCREEN_LANGS]
LONG_GENRES = [
    (28, "Akční"),
    (12, "Dobrodružný"),
    (16, "Animovaný"),
    (99, "Dokumentární"),
    (18, "Drama"),
    (10751, "Rodinný"),
    (36, "Historický"),
    (9648, "Mysteriózní"),
    (878, "Vědeckofantastický"),
    (10752, "Válečný"),
]
PROFILE_NAME = "Babička Květoslava"

# -- what the browser measures ----------------------------------------------------------

AUDIT = r"""
(opts) => {
  const out = { overflow: [], wide: [], targets: [], tiny: [], inputs: [], clipped: [],
                covered: [], tabbar: [], safe: [] };
  const vw = document.documentElement.clientWidth;
  const vh = window.innerHeight;
  const probe = document.createElement("div");
  probe.style.cssText = "position:fixed;visibility:hidden;padding:env(safe-area-inset-top) " +
    "env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left)";
  document.body.append(probe);
  const p = getComputedStyle(probe);
  const inset = { top: parseFloat(p.paddingTop), right: parseFloat(p.paddingRight),
                  bottom: parseFloat(p.paddingBottom), left: parseFloat(p.paddingLeft) };
  probe.remove();
  const root = document.querySelector(opts.root || "body");
  const exempt = (n, list) => list && list.length && n.closest(list.join(","));
  const label = (n) => {
    let s = n.tagName.toLowerCase();
    if (n.id) s += "#" + n.id;
    else if (n.classList.length) s += "." + [...n.classList].slice(0, 2).join(".");
    const text = (n.getAttribute("aria-label") || n.textContent || n.value || "")
      .trim().replace(/\s+/g, " ").slice(0, 32);
    return text ? `${s} "${text}"` : s;
  };
  const shown = (n) => n.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })
    && !n.closest(".sr-only, .skip-link");
  const box = (n) => n.getBoundingClientRect();
  const scrollerX = (n) => {
    for (let a = n.parentElement; a && a !== document.body; a = a.parentElement) {
      const o = getComputedStyle(a).overflowX;
      if (o === "auto" || o === "scroll") return a;
    }
    return null;
  };
  const scrollerY = (n) => {
    for (let a = n.parentElement; a && a !== document.body; a = a.parentElement) {
      const o = getComputedStyle(a).overflowY;
      if ((o === "auto" || o === "scroll") && a.scrollHeight > a.clientHeight) return a;
    }
    return null;
  };
  const fixed = (n) => {
    for (let a = n; a && a !== document.body; a = a.parentElement) {
      const s = getComputedStyle(a).position;
      if (s === "fixed" || s === "sticky") return a;
    }
    return null;
  };
  const ownText = (n) => [...n.childNodes].some((c) => c.nodeType === 3 && /\S/.test(c.data));
  const TARGETS = "a[href], button, input:not([type=hidden]), select, textarea, summary, " +
    "[role=button], [role=slider], [role=tab], [role=menuitemradio]";
  const nodes = [...root.querySelectorAll("*")].filter(
    (n) => !(n instanceof SVGElement) && n.tagName !== "OPTION" && shown(n));
  const targets = [...root.querySelectorAll(TARGETS)].filter((n) => shown(n) && getComputedStyle(n).pointerEvents !== "none");
  const texts = nodes.filter(ownText);

  window.scrollTo(0, 0);
  const scroll = document.scrollingElement;
  if (scroll.scrollWidth > innerWidth) out.overflow.push(`page is ${scroll.scrollWidth}px wide on ${innerWidth}px`);

  // wide: only the outermost element that sticks out is reported
  const wide = new Set();
  for (const n of nodes) {
    const r = box(n);
    if (r.width < 1 || r.height < 1 || (r.left >= -1 && r.right <= vw + 1)) continue;
    if (scrollerX(n)) continue;
    if (n.parentElement && wide.has(n.parentElement)) { wide.add(n); continue; }
    wide.add(n);
    out.wide.push(`${label(n)} ${Math.round(r.left)}..${Math.round(r.right)}`);
  }

  // targets: inline links in running text and the checkbox inside its label are measured
  // as people tap them
  for (let n of targets) {
    if (exempt(n, opts.targetsOk)) continue;
    if (n.matches("input[type=checkbox], input[type=radio]") && n.closest("label")) n = n.closest("label");
    if (n.matches("#nav-search-input")) n = n.form;  // a tap anywhere on the pill opens it
    if (getComputedStyle(n).display === "inline" && n.parentElement && ownText(n.parentElement)) continue;
    const r = box(n);
    if (r.width < 39.5 || r.height < 39.5) out.targets.push(`${label(n)} ${Math.round(r.width)}x${Math.round(r.height)}`);
  }

  // tiny text
  const tiny = new Set();
  for (const n of texts) {
    if (exempt(n, opts.tinyOk)) continue;
    const size = parseFloat(getComputedStyle(n).fontSize);
    if (size < 10.95 && !tiny.has(n)) { tiny.add(n); out.tiny.push(`${label(n)} ${size}px`); }
  }

  // inputs iOS would zoom into
  for (const n of root.querySelectorAll("input, select, textarea")) {
    if (n.matches("[type=hidden], [type=checkbox], [type=radio], [type=range], [type=submit], [type=button]")) continue;
    if (!n.checkVisibility() || n.closest(".sr-only")) continue;
    const size = parseFloat(getComputedStyle(n).fontSize);
    if (size < 16) out.inputs.push(`${label(n)} ${size}px`);
  }

  // clipped: a control's label cut off with an ellipsis, spilling out, or wrapped
  const lines = (n) => {
    const tops = [];
    const walker = document.createTreeWalker(n, NodeFilter.SHOW_TEXT);
    for (let t = walker.nextNode(); t; t = walker.nextNode()) {
      if (!/\S/.test(t.data)) continue;
      const range = document.createRange();
      range.selectNodeContents(t);
      for (const r of range.getClientRects()) if (r.width > 1 && !tops.some((x) => Math.abs(x - r.top) < 4)) tops.push(r.top);
    }
    return tops.length;
  };
  const CONTROLS = "button, .btn, .chip, .tool-btn, .ctl, .filter-pill, .tabbar a, .nav, .menu-head, .sheet-head";
  for (const n of nodes) {
    if (!n.closest(CONTROLS) || exempt(n, opts.clipOk)) continue;
    const s = getComputedStyle(n);
    const clamps = s.textOverflow === "ellipsis" || s.webkitLineClamp !== "none";
    if (clamps && (n.scrollWidth > n.clientWidth + 1 || n.scrollHeight > n.clientHeight + 1)) {
      out.clipped.push(`${label(n)} cut off (${n.clientWidth}px for ${n.scrollWidth}px)`);
    } else if (n.matches(TARGETS + ", .chip, .filter-pill") && s.overflowX === "visible" && n.scrollWidth > n.clientWidth + 2) {
      out.clipped.push(`${label(n)} spills out (${n.clientWidth}px for ${n.scrollWidth}px)`);
    }
  }
  for (const n of root.querySelectorAll(".chip, .filter-pill, .tabbar a span, .ctl-text span, .tool-btn, .version-play, .menu-head strong")) {
    if (shown(n) && lines(n) > 1) out.clipped.push(`${label(n)} wraps to ${lines(n)} lines`);
  }
  for (const n of root.querySelectorAll(".btn")) {
    if (shown(n) && lines(n) > 2) out.clipped.push(`${label(n)} wraps to ${lines(n)} lines`);
  }

  // clipped: a badge or label placed over a card sticks out of it sideways
  for (const n of nodes) {
    if (getComputedStyle(n).position !== "absolute" || exempt(n, opts.clipOk)) continue;
    const card = n.offsetParent;
    if (!card || card === document.body || getComputedStyle(card).overflowX !== "visible") continue;
    const r = box(n), c = box(card);
    if (r.left < c.left - 1 || r.right > c.right + 1) out.clipped.push(`${label(n)} sticks out of ${label(card).slice(0, 30)} (${Math.round(r.left)}..${Math.round(r.right)} in ${Math.round(c.left)}..${Math.round(c.right)})`);
  }

  // covered: the control's centre belongs to something else
  const nav = document.querySelector(".nav"), tabbar = document.querySelector(".tabbar");
  for (const n of targets) {
    const r = box(n);
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    if (x < 0 || y < 0 || x >= vw || y >= vh) continue;
    const sx = scrollerX(n), sy = scrollerY(n);
    for (const s of [sx, sy]) {
      if (!s) continue;
      const b = box(s);
      if (x < b.left || x > b.right || y < b.top || y > b.bottom) { n.dataset.auditOut = "1"; }
    }
    if (n.dataset.auditOut) { delete n.dataset.auditOut; continue; }
    const hit = document.elementFromPoint(x, y);
    if (!hit || n.contains(hit) || hit.contains(n) || (n.labels && [...n.labels].some((l) => l.contains(hit)))) continue;
    if (!fixed(n) && ((nav && nav.contains(hit)) || (tabbar && tabbar.contains(hit)))) continue;
    if (n.closest("label") && n.closest("label").contains(hit)) continue;
    out.covered.push(`${label(n)} under ${label(hit)}`);
  }

  // the tab bar
  if (tabbar && getComputedStyle(tabbar).display !== "none" && !opts.overlay) {
    const links = [...tabbar.querySelectorAll("a")];
    for (const a of links) {
      const r = box(a);
      if (r.bottom > vh - inset.bottom + 0.5) out.tabbar.push(`${label(a)} under the home indicator (${Math.round(r.bottom)} > ${vh - inset.bottom})`);
      if (r.left < inset.left - 0.5 || r.right > vw - inset.right + 0.5) out.tabbar.push(`${label(a)} under the notch`);
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      if (hit && !tabbar.contains(hit)) out.tabbar.push(`${label(a)} covered by ${label(hit)}`);
    }
    window.scrollTo(0, scroll.scrollHeight);
    const top = box(tabbar).top;
    let last = null, bottom = -Infinity;
    for (const n of nodes) {
      if (fixed(n) || !(ownText(n) || n.matches(TARGETS + ", img"))) continue;
      const b = box(n).bottom;
      if (b > bottom) { bottom = b; last = n; }
    }
    if (last && bottom > top + 0.5) out.tabbar.push(`end of the page ${label(last)} ${Math.round(bottom - top)}px behind the tab bar`);
    window.scrollTo(0, 0);
  }

  // safe areas: text by its glyphs (padding may go under the notch), targets by their box;
  // a scrolling menu or sheet is judged scrolled to its end
  const textBox = (n) => {
    const range = document.createRange();
    range.selectNodeContents(n);
    const rects = [...range.getClientRects()].filter((r) => r.width > 0 && r.height > 0);
    if (!rects.length) return box(n);
    return { left: Math.min(...rects.map((r) => r.left)), right: Math.max(...rects.map((r) => r.right)),
             top: Math.min(...rects.map((r) => r.top)), bottom: Math.max(...rects.map((r) => r.bottom)) };
  };
  if (inset.top || inset.right || inset.bottom || inset.left) {
    const seen = new Set();
    const scrollers = new Set();
    const place = (r) => `(${Math.round(r.left)},${Math.round(r.top)} ${Math.round(r.right - r.left)}x${Math.round(r.bottom - r.top)})`;
    for (const n of [...targets, ...texts]) {
      if (seen.has(n) || exempt(n, opts.safeOk)) continue;
      seen.add(n);
      const r = targets.includes(n) ? box(n) : textBox(n);
      if (r.right - r.left < 1 || r.bottom - r.top < 1) continue;
      const sx = scrollerX(n), sy = scrollerY(n);
      let problem = "";
      if (r.left < inset.left - 0.5) problem = "left notch";
      else if (!sx && r.right > vw - inset.right + 0.5) problem = "right notch";
      else if (fixed(n) && sy) scrollers.add(sy);
      else if (fixed(n) && r.top < inset.top - 0.5 && r.bottom > 0) problem = "status bar";
      else if (fixed(n) && r.bottom > vh - inset.bottom + 0.5 && r.top < vh) problem = "home indicator";
      if (problem) out.safe.push(`${label(n)} under the ${problem} ${place(r)}`);
    }
    for (const s of scrollers) {
      const before = s.scrollTop;
      s.scrollTop = s.scrollHeight;
      const top = box(s).top;
      for (const n of [...seen].filter((n) => s.contains(n))) {
        const r = targets.includes(n) ? box(n) : textBox(n);
        if (r.bottom > vh - inset.bottom + 0.5 && r.top < vh) out.safe.push(`${label(n)} under the home indicator at the end of ${label(s).slice(0, 30)} ${place(r)}`);
        else if (r.top < inset.top - 0.5 && r.top >= top - 1) out.safe.push(`${label(n)} under the status bar ${place(r)}`);
      }
      s.scrollTop = before;
    }
  }
  out.inset = inset;
  return out;
}
"""

# Accepted on purpose (each with its reason):
TARGETS_OK = [
    ".episode-name button",  # the whole episode row is the target; the button is for keyboards
    ".nav-version",  # the version pill next to the logo: information, its link is a bonus
    ".footer a",  # links in the footer's sentences
    ".about-links a",  # text links at the end of the detail page, 24px apart
    ".key-remove",  # an underlined text link under a form
]
TINY_OK = [
    ".flag",  # language codes drawn inside 24x16 flag badges are icons, not text
]
CLIP_OK = [
    ".player-heading",  # the title over the video is ellipsised by design
    ".seek-thumb",  # the thumb is centred on the ends of the seek bar
    ".seek-tooltip",
]
CATEGORIES = (
    "overflow",
    "wide",
    "targets",
    "tiny",
    "inputs",
    "clipped",
    "covered",
    "tabbar",
    "safe",
)


def audit(page, **opts) -> dict[str, list[str]]:
    opts = {"targetsOk": TARGETS_OK, "tinyOk": TINY_OK, "clipOk": CLIP_OK, **opts}
    return page.evaluate(AUDIT, opts)


SETTLED = """document.getAnimations().every(
    (a) => a.playState !== "running" || a.effect.getTiming().iterations === Infinity)"""


def check(page, where: str, **opts):
    """Measure the page (or the overlay `root`) and fail with every problem found."""
    page.wait_for_function(SETTLED)  # sheets slide in: measure where they end up
    # the search page focuses the header's search, which opens over the header on tablets
    page.evaluate(
        "document.activeElement?.id === 'nav-search-input' && document.activeElement.blur()"
    )
    result = audit(page, **opts)
    found = [f"{c}: {item}" for c in CATEGORIES for item in result[c]]
    assert not found, f"{where} (insets {result['inset']}):\n  " + "\n  ".join(found)


# -- one browser context per screen and language, for the whole file ----------------------


@pytest.fixture(scope="module")
def household(app_server, torrserver, tmp_path_factory):
    """Shared progress in a temporary file, a second profile, Czech genres with long names."""
    from streamfinder import shared

    svc = app_server["services"]
    genres = {"genres": [{"id": i, "name": n} for i, n in LONG_GENRES]}
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            shared, "PATH", str(tmp_path_factory.mktemp("household") / "shared.json")
        )
        mp.setitem(svc.tmdb.data, "/genre/movie/list", genres)
        mp.setitem(svc.tmdb.data, "/genre/tv/list", genres)
        torrserver.reset()
        client = app_server["app"].test_client()
        reply = client.post(
            "/api/profiles", json={"action": "add", "name": PROFILE_NAME, "color": 3}
        )
        assert reply.status_code == 200, reply.data
        yield app_server["url"]


def _storage(now: int) -> dict:
    return {
        "streamfinder.profile.chosen": "1",
        "streamfinder.progress.v1": json.dumps(
            {
                # Continue watching (not the film the player tests play: 20 s long)
                f"movie:{fakes.FUTURE_ID}": {
                    "title": "Future Film",
                    "url": f"/movie/{fakes.FUTURE_ID}",
                    "backdrop": "https://image.tmdb.org/t/p/w780/backdrop101.jpg",
                    "position": 600,
                    "duration": 6000,
                    "updated": now,
                },
                f"tv:{fakes.UPCOMING_ID}": {
                    "title": "Upcoming Show",
                    "url": f"/show/{fakes.UPCOMING_ID}",
                    "season": 1,
                    "episode": 2,
                    "updated": now - 1000,
                },
            }
        ),
    }


def new_screen(browser, url, name: str, lang: str, admin=False, script=""):
    """A browser context like that phone (touch, its pixel density, its notch) and a page."""
    (width, height), scale, insets = SCREENS[name]
    context = browser.new_context(
        locale="cs-CZ" if lang == "cs" else "en-US",
        viewport={"width": width, "height": height},
        screen={"width": width, "height": height},
        device_scale_factor=scale,
        is_mobile=True,
        has_touch=True,
        user_agent=TABLET_UA if width >= 768 else PIXEL_UA,
    )
    context.add_cookies([{"name": "lang", "value": lang, "url": url}])
    context.route(lambda u: not u.startswith("http://127.0.0.1"), _offline)
    context.set_default_timeout(10_000)
    storage = json.dumps(_storage(int(time.time() * 1000)))
    context.add_init_script(
        f"for (const [k, v] of Object.entries({storage}))"
        " if (localStorage.getItem(k) === null) localStorage.setItem(k, v);" + script
    )
    page = Page(context.new_page(), url)
    if insets:
        cdp = context.new_cdp_session(page.page)
        cdp.send("Emulation.setSafeAreaInsetsOverride", {"insets": insets})
    if admin:
        page.login_admin()
    return context, page


@pytest.fixture(scope="module")
def screens(browser, household):
    """screens(name, lang, admin=False) -> a Page on that phone, made once and reused."""
    made: dict[tuple, tuple] = {}

    def get(name: str, lang: str, admin: bool = False) -> Page:
        key = (name, lang, admin)
        if key not in made:
            made[key] = new_screen(browser, household, name, lang, admin)
        page = made[key][1]
        page.errors.clear()
        return page

    yield get
    for context, _ in made.values():
        context.close()


# -- pages ------------------------------------------------------------------------------


def _ready_home(page):
    page.locator("#continue-row:not([hidden]) .wide-card").first.wait_for()


def _ready_detail(page):
    page.wait_for_function("!document.getElementById('play-btn').disabled")


def _ready_show(page):
    _ready_detail(page)
    page.locator("#episode-list li").first.wait_for()


def _ready_calendar(page):
    page.locator("#calendar .calendar-day").first.wait_for()


def _ready_status(page):
    page.wait_for_function(
        "document.getElementById('library-summary').textContent.trim() !== ''"
    )


def _ready_settings(page):
    page.locator("#settings-form").wait_for()


PAGES = {
    "home": ("/", _ready_home, False),
    "movies": ("/movies?genre=18&sort=rating&decade=2020&lang=cs", None, False),
    "shows": ("/shows", None, False),
    "search": ("/search?q=test", None, False),
    "movie": (f"/movie/{fakes.MOVIE_ID}", _ready_detail, False),
    "show": (f"/show/{fakes.SHOW_ID}", _ready_show, False),
    "calendar": ("/calendar", _ready_calendar, False),
    "settings": ("/settings", _ready_settings, False),
    "settings-admin": ("/settings", _ready_settings, True),
    "status": ("/status", _ready_status, True),
}


@pytest.mark.parametrize("screen, lang", SCREEN_LANGS, ids=SCREEN_IDS)
@pytest.mark.parametrize("name", list(PAGES))
def test_page_on_phone(screens, name, screen, lang):
    path, ready, admin = PAGES[name]
    page = screens(screen, lang, admin)
    page.open(path)
    if ready:
        ready(page)
    check(page, f"{name} {lang} {screen}")
    assert not page.errors, "\n".join(page.errors)


@pytest.mark.parametrize("screen, lang", SCREEN_LANGS, ids=SCREEN_IDS)
def test_profile_picker_on_phone(screens, screen, lang):
    page = screens(screen, lang)
    page.open("/search")
    page.locator("#profile-btn").wait_for()
    check(page, f"header {lang} {screen}")  # the header with the profile button
    page.locator("#profile-btn").tap()
    sheet = page.locator(".profile-sheet")
    sheet.wait_for()
    check(page, f"profile picker {lang} {screen}", root=".profile-sheet", overlay=True)
    page.locator(".profile-actions .btn").first.tap()  # add a profile: the form
    page.locator(".profile-form input").wait_for()
    check(page, f"profile form {lang} {screen}", root=".profile-sheet", overlay=True)
    page.keyboard.press("Escape")


HOVER_ONLY = r"""
() => {
  // Every `X:hover Y` rule in force; Y (shown on screen) must not be invisible at rest.
  const found = new Set();
  const walk = (rules) => {
    for (const rule of rules) {
      if (rule instanceof CSSMediaRule) { if (matchMedia(rule.media.mediaText).matches) walk(rule.cssRules); continue; }
      if (!(rule instanceof CSSStyleRule) || !rule.selectorText.includes(":hover")) continue;
      for (const selector of rule.selectorText.split(",")) {
        if (!selector.includes(":hover")) continue;
        const rest = selector.replace(/:hover/g, "").trim();
        let nodes = [];
        try { nodes = document.querySelectorAll(rest); } catch { continue; }
        for (const n of nodes) {
          if (!n.checkVisibility()) continue;
          const s = getComputedStyle(n);
          if (Number(s.opacity) === 0 || s.visibility === "hidden" || /^matrix\(0, 0, 0, 0,/.test(s.transform))
            found.add(`${rest} (shown by ${selector.trim()})`);
        }
      }
    }
  };
  for (const sheet of document.styleSheets) {
    try { walk(sheet.cssRules); } catch { /* another origin's stylesheet */ }
  }
  return [...found];
}
"""


@pytest.mark.parametrize("screen", ["390x844", "768x1024"])
@pytest.mark.parametrize("name", ["home", "show"])
def test_nothing_needs_hovering(screens, name, screen):
    """Row arrows, the remove button on cards, play badges: what a mouse finds by hovering
    is either always there on a touch screen or not needed (the rows scroll by swiping)."""
    path, ready, admin = PAGES[name]
    page = screens(screen, "cs", admin)
    page.open(path)
    ready(page)
    hidden = page.evaluate(HOVER_ONLY)
    assert not hidden, "only reachable by hovering:\n  " + "\n  ".join(hidden)


def test_tablet_header_search_opens_over_the_header(screens):
    """Tablets: the header's search may shrink to its icon; tapped, it spans the header."""
    page = screens("768x1024", "cs")
    page.open("/movies")
    pill = page.locator(".nav-search")
    icon = pill.locator(".icon").bounding_box()
    page.touchscreen.tap(icon["x"] + icon["width"] / 2, icon["y"] + icon["height"] / 2)
    page.wait_for_function("document.activeElement.id === 'nav-search-input'")
    page.wait_for_function(SETTLED)
    box, nav = pill.bounding_box(), page.locator(".nav").bounding_box()
    assert box["width"] > nav["width"] * 0.8, (box, nav)
    assert nav["y"] <= box["y"] and box["y"] + box["height"] <= nav["y"] + nav["height"]
    page.locator("#nav-search-input").fill("test")
    page.keyboard.press("Enter")
    page.wait_for_url("**/search?q=test")


# -- the player -------------------------------------------------------------------------

PLAYER_SCREENS = ["320x568", "390x844", "844x390"]
FITS = r"""
(selector) => {
  const n = document.querySelector(selector);
  const r = n.getBoundingClientRect();
  const s = getComputedStyle(n);
  return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, vh: innerHeight,
           vw: innerWidth, scrolls: s.overflowY === "auto" || s.overflowY === "scroll" };
}
"""
PAUSED = "document.getElementById('video').paused"
IDLE = "document.getElementById('player').classList.contains('is-idle')"


@pytest.fixture
def h264_screens(screens, h264):
    if not h264:
        pytest.skip("this Chromium build can't play H.264/AAC")
    return screens


def open_player(page, path):
    page.open(path)
    _ready_detail(page)
    page.locator("#play-btn").tap()
    page.locator("#player").wait_for(state="visible")
    wait_playing(page)


def show_controls(page):
    """A touch on the title bar wakes the controls without touching the video."""
    box = page.locator(".player-heading").bounding_box()
    page.touchscreen.tap(box["x"] + 4, box["y"] + box["height"] / 2)
    page.wait_for_function(f"!{IDLE}")


def assert_fits(page, selector, where):
    """The menu or sheet is inside the screen; taller content scrolls inside it."""
    box = page.evaluate(FITS, selector)
    inside = box["top"] >= -0.5 and box["bottom"] <= box["vh"] + 0.5
    inside = inside and box["left"] >= -0.5 and box["right"] <= box["vw"] + 0.5
    assert inside, f"{where}: {selector} is not inside the screen: {box}"
    assert box["scrolls"], f"{where}: {selector} doesn't scroll inside"


def open_menu(page, button, menu, where):
    page.wait_for_function(f"!document.getElementById('{button}').disabled")
    page.locator(f"#{button}").tap()
    page.locator(f"#{menu}").wait_for()
    check(page, f"{menu} {where}", root=f"#{menu}", overlay=True)
    assert_fits(page, f"#{menu}", where)
    page.keyboard.press("Escape")
    page.locator(f"#{menu}").wait_for(state="hidden")


@pytest.mark.parametrize("screen", PLAYER_SCREENS)
def test_movie_player_on_phone(h264_screens, screen):
    page = h264_screens(screen, "cs")
    where = f"cs {screen}"
    open_player(page, f"/movie/{fakes.MOVIE_ID}")
    assert page.locator("#video").get_attribute("playsinline") is not None
    show_controls(page)
    check(page, f"player {where}", root="#player", overlay=True)
    # The seek bar's thumb shows without hovering, big enough to grab.
    thumb = page.locator("#seek-thumb").bounding_box()
    assert min(thumb["width"], thumb["height"]) >= 14, f"seek thumb {thumb}"
    hidden = page.evaluate(HOVER_ONLY)
    assert not hidden, f"only reachable by hovering: {hidden}"
    open_menu(page, "subs-btn", "subs-menu", where)

    show_controls(page)
    page.locator("#player-versions").tap()
    page.locator("#versions-list li, #versions-picks .pick").first.wait_for()
    page.locator(".versions-advanced summary").tap()
    page.locator("#manual-query").wait_for()
    check(page, f"versions {where}", root="#versions", overlay=True)
    assert_fits(page, ".sheet-body", where)
    page.locator("#versions .sheet-head [data-close]").tap()
    page.locator("#versions").wait_for(state="hidden")
    # Android's Back button (a history step) closes the player.
    page.go_back()
    page.locator("#player").wait_for(state="hidden")
    assert not page.errors, "\n".join(page.errors)


@pytest.mark.parametrize("screen", PLAYER_SCREENS)
def test_show_player_menus_on_phone(h264_screens, screen):
    page = h264_screens(screen, "cs")
    where = f"cs {screen}"
    open_player(page, f"/show/{fakes.SHOW_ID}")
    page.locator("#audio-btn").wait_for()
    show_controls(page)
    check(page, f"show player {where}", root="#player", overlay=True)
    open_menu(page, "audio-btn", "audio-menu", where)
    show_controls(page)
    open_menu(page, "episodes-btn", "episodes-menu", where)
    page.go_back()
    page.locator("#player").wait_for(state="hidden")
    assert not page.errors, "\n".join(page.errors)


# Touch gestures the player doesn't know yet: player.js is being split into modules, the
# changes are in the mobile audit report.


def test_tap_on_video_shows_and_hides_controls(h264_screens):
    page = h264_screens("390x844", "cs")
    open_player(page, f"/movie/{fakes.MOVIE_ID}")
    page.evaluate("document.getElementById('player').classList.add('is-idle')")
    box = page.locator("#video").bounding_box()
    centre = (box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.touchscreen.tap(*centre)
    page.wait_for_timeout(400)
    assert not page.evaluate(IDLE) and not page.evaluate(PAUSED), "1st tap: controls"
    page.touchscreen.tap(*centre)
    page.wait_for_timeout(400)
    assert page.evaluate(IDLE) and not page.evaluate(PAUSED), "2nd tap: controls away"


def test_double_tap_seeks(h264_screens):
    page = h264_screens("844x390", "cs")
    open_player(page, f"/movie/{fakes.MOVIE_ID}")
    before = page.evaluate("document.getElementById('video').currentTime")
    box = page.locator("#video").bounding_box()
    x, y = box["x"] + box["width"] * 0.85, box["y"] + box["height"] / 2
    page.touchscreen.tap(x, y)
    page.wait_for_timeout(80)
    page.touchscreen.tap(x, y)
    page.wait_for_function(
        f"document.getElementById('video').currentTime > {before + 7}", timeout=3000
    )
    assert not page.evaluate(PAUSED)


def test_swipe_down_closes_the_player(h264_screens):
    page = h264_screens("390x844", "cs")
    open_player(page, f"/movie/{fakes.MOVIE_ID}")
    cdp = page.context.new_cdp_session(page.page)
    touch = lambda kind, points: cdp.send(
        "Input.dispatchTouchEvent", {"type": kind, "touchPoints": points}
    )
    touch("touchStart", [{"x": 195, "y": 300}])
    for y in range(330, 660, 30):
        touch("touchMove", [{"x": 195, "y": y}])
    touch("touchEnd", [])
    page.locator("#player").wait_for(state="hidden", timeout=3000)


def test_fullscreen_button_on_an_iphone(browser, household, h264):
    if not h264:
        pytest.skip("this Chromium build can't play H.264/AAC")
    iphone = """
        delete Element.prototype.requestFullscreen;
        delete Element.prototype.webkitRequestFullscreen;
        HTMLVideoElement.prototype.webkitEnterFullscreen = function () { window.nativeFullscreen = true; };
    """
    context, page = new_screen(browser, household, "390x844", "cs", script=iphone)
    try:
        open_player(page, f"/movie/{fakes.MOVIE_ID}")
        show_controls(page)
        page.locator("#ctl-fullscreen").tap()
        page.wait_for_function("window.nativeFullscreen === true", timeout=2000)
    finally:
        context.close()
