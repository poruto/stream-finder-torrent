"""HTML pages and the presentation helpers shared with the JSON API."""

import logging
import math
import re
import time
from typing import Any
from urllib.parse import quote_plus, urlparse

from flask import (
    Blueprint,
    abort,
    make_response,
    redirect,
    render_template,
    request,
    url_for,
)

from . import auth, config
from .i18n import COOKIE, LANGUAGES, current_language, plural, t
from .matching import normalize_title
from .services import services
from .subtitles import LANGUAGE_NAMES
from .tmdb import TMDBError, image_url, trailer_url

logger = logging.getLogger(__name__)
web = Blueprint("web", __name__)

# Pages that work without a TMDB key (everything else needs it).
_NO_KEY_ENDPOINTS = {"web.settings", "web.status", "web.set_language"}


@web.before_request
def require_tmdb_key():
    if not config.TMDB_API_KEY and request.endpoint not in _NO_KEY_ENDPOINTS:
        return redirect(url_for("web.settings", setup=1))


MEDIA_SLUG = {"movie": "film", "tv": "serial"}
# CJK, Hangul, Cyrillic, Arabic, Thai... – titles TMDB has no Czech/English name for.
_NON_LATIN_RE = re.compile(r"[Ѐ-ӿ؀-ۿ฀-๿぀-ヿ㐀-鿿가-힯]")
_IMDB_RE = re.compile(r"tt\d{5,10}")
_TRAILING_YEAR_RE = re.compile(r"^(.+?)\s+\(?((?:19|20)\d{2})\)?$")

HOME_ROWS = [
    ("trending", "row_trending", "/trending/all/week", {}),
    ("popular_movies", "row_popular_movies", "/movie/popular", {}),
    ("popular_tv", "row_popular_tv", "/tv/popular", {}),
    ("top_movies", "row_top_movies", "/movie/top_rated", {}),
    ("top_tv", "row_top_tv", "/tv/top_rated", {}),
    (
        "action",
        "row_action",
        "/discover/movie",
        {"with_genres": 28, "sort_by": "popularity.desc"},
    ),
    (
        "comedy",
        "row_comedy",
        "/discover/movie",
        {"with_genres": 35, "sort_by": "popularity.desc"},
    ),
    (
        "animation",
        "row_animation",
        "/discover/movie",
        {"with_genres": 16, "sort_by": "popularity.desc"},
    ),
]

BROWSE_SORTS = {
    "popular": "popularity.desc",
    "rating": "vote_average.desc",
    "new": "primary_release_date.desc",
}
IMDB_FILTERS = (6, 7, 8)
BROWSE_DECADES = ("2020", "2010", "2000", "1990", "1980", "older")
# UI key -> TMDB with_original_language ("|" = or)
BROWSE_LANGUAGES = {
    "cs": "cs|sk",
    "en": "en",
    "ko": "ko",
    "ja": "ja",
    "es": "es",
    "fr": "fr",
    "de": "de",
}


def browse_filters(args) -> dict[str, Any]:
    """Validated catalogue filters from query args (shared by the page and the API)."""
    imdb = args.get("imdb", type=int)
    return {
        "genre": args.get("genre", type=int),
        "sort": args.get("sort") if args.get("sort") in BROWSE_SORTS else "popular",
        "imdb": imdb if imdb in IMDB_FILTERS else None,
        "decade": args.get("decade") if args.get("decade") in BROWSE_DECADES else None,
        "lang": args.get("lang") if args.get("lang") in BROWSE_LANGUAGES else None,
    }


BROWSE_PAGE_TARGET = (
    18  # with an IMDb filter, keep fetching TMDB pages until a page is this full
)
BROWSE_MAX_TMDB_PAGES = 4


def year_of(data: dict[str, Any]) -> int | None:
    date = data.get("release_date") or data.get("first_air_date") or ""
    return int(date[:4]) if date[:4].isdigit() else None


def detail_url(media_type: str, tmdb_id: int) -> str:
    return url_for(
        "web.movie" if media_type == "movie" else "web.show", tmdb_id=tmdb_id
    )


def is_released(data: dict[str, Any]) -> bool:
    """Whether the premiere date has passed; without a date, trust TMDB's status."""
    date = data.get("release_date") or data.get("first_air_date")
    if date:
        return date <= time.strftime("%Y-%m-%d")
    return data.get("status") in ("Released", "Returning Series", "Ended", "Canceled")


_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def coming_label(data: dict[str, Any]) -> str | None:
    """ "Out 18 Dec 2026" / "Vyjde 18. 12. 2026" for a title that isn't out yet."""
    date = data.get("release_date") or data.get("first_air_date") or ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or is_released(data):
        return None
    year, month, day = int(date[:4]), int(date[5:7]), int(date[8:])
    if current_language() == "cs":
        when = f"{day}. {month}. {year}"
    else:
        when = f"{day} {_MONTHS[month - 1]} {year}"
    return t("coming_on", date=when)


# Widths offered for the big hero backdrops; "original" is at least Full HD on TMDB.
_BACKDROP_WIDTHS = (("w780", 780), ("w1280", 1280), ("original", 1920))


def backdrop_srcset(path: str | None) -> str | None:
    """`srcset` for a hero backdrop, so phones don't download the original (often 4K) file."""
    if not path:
        return None
    return ", ".join(
        f"{image_url(path, size)} {width}w" for size, width in _BACKDROP_WIDTHS
    )


# Widths offered for posters: rows and grids are a third of a phone's width and up to
# 210px on big screens (`sizes` in _macros.html and app.js, after app.css .row-track/.grid).
_POSTER_WIDTHS = (185, 342, 500)
_POSTER_SIZE_RE = re.compile(r"/t/p/w\d+/")


@web.app_template_global()
def poster_srcset(url: str | None) -> str | None:
    """`srcset` for a TMDB poster URL (any size), so phones and TVs fetch one that fits."""
    if not url or not _POSTER_SIZE_RE.search(url):
        return None
    return ", ".join(
        f"{_POSTER_SIZE_RE.sub(f'/t/p/w{width}/', url, count=1)} {width}w"
        for width in _POSTER_WIDTHS
    )


def card(
    result: dict[str, Any], media_type: str | None = None
) -> dict[str, Any] | None:
    """Compact representation of a TMDB list item for poster rows and grids."""
    media_type = result.get("media_type") or media_type
    if media_type not in ("movie", "tv") or not result.get("poster_path"):
        return None
    return {
        "id": result["id"],
        "type": media_type,
        "title": result.get("title") or result.get("name") or "",
        "year": year_of(result),
        "rating": round(result.get("vote_average") or 0, 1),
        "imdb": None,
        "coming": coming_label(result),
        "poster": image_url(result.get("poster_path"), "w342"),
        "backdrop": image_url(result.get("backdrop_path"), "w780"),
        "url": detail_url(media_type, result["id"]),
    }


def cards(
    results: list[dict[str, Any]], media_type: str | None = None
) -> list[dict[str, Any]]:
    seen, items = set(), []
    for result in results:
        item = card(result, media_type)
        if item and (item["type"], item["id"]) not in seen:
            seen.add((item["type"], item["id"]))
            items.append(item)
    return items


def with_imdb(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach IMDb ratings (needs one cached TMDB lookup per item for the IMDb id)."""
    svc = services()
    if not svc.imdb.ready or not items:
        return items
    ids = svc.tmdb.imdb_ids([(i["type"], i["id"]) for i in items])
    ratings = svc.imdb.many(list(ids.values()))
    for item in items:
        rating = ratings.get(ids.get((item["type"], item["id"])))
        item["imdb"] = rating[0] if rating else None
    return items


def browse_page(media_type: str, filters: dict[str, Any], page: int) -> dict[str, Any]:
    """One page of the catalogue. With an IMDb filter several TMDB pages may be merged."""
    tmdb = services().tmdb
    imdb_min = filters["imdb"]
    items: list[dict[str, Any]] = []
    total_pages = 1
    tmdb_page = page
    for _ in range(BROWSE_MAX_TMDB_PAGES if imdb_min else 1):
        data = tmdb.discover(
            media_type,
            genre=filters["genre"],
            sort_by=BROWSE_SORTS[filters["sort"]],
            page=tmdb_page,
            min_votes=200 if imdb_min else None,
            decade=filters["decade"],
            language=BROWSE_LANGUAGES.get(filters["lang"]),
        )
        total_pages = min(data.get("total_pages") or 1, 500)
        batch = cards(data.get("results", []), media_type)
        if imdb_min:
            batch = [i for i in with_imdb(batch) if (i["imdb"] or 0) >= imdb_min]
        items += batch
        tmdb_page += 1
        if len(items) >= BROWSE_PAGE_TARGET or tmdb_page > total_pages:
            break
    return {
        "items": items,
        "next_page": tmdb_page if tmdb_page <= total_pages else None,
    }


def search_items(query: str, kind: str | None = None) -> list[dict[str, Any]]:
    """Movie/TV search: IMDb links, "title year", people (their known films), better ranking."""
    tmdb = services().tmdb
    imdb = _IMDB_RE.search(query)
    if imdb:
        return cards(tmdb.find_imdb(imdb.group(0)))

    year = None
    match = _TRAILING_YEAR_RE.match(query)
    if match:
        query, year = match.group(1), int(match.group(2))

    wanted = normalize_title(query)
    data = tmdb.search_multi(query)
    results, people_works = [], []
    for result in data.get("results", []):
        if result.get("media_type") != "person":
            results.append(result)
        elif len(wanted) >= 4 and any(
            part.startswith(wanted) or wanted.startswith(part)
            for part in [normalize_title(result.get("name") or "")]
            + normalize_title(result.get("name") or "").split()
        ):
            # "nolan" -> his films, but only when the query really is that person's name.
            people_works += result.get("known_for") or []
    if (data.get("total_pages") or 1) > 1 and len(results) < 20:
        results += [
            r
            for r in tmdb.search_multi(query, page=2).get("results", [])
            if r.get("media_type") != "person"
        ]

    def rank(result: dict[str, Any]) -> tuple:
        titles = {
            normalize_title(result.get(k) or "")
            for k in ("title", "name", "original_title", "original_name")
        }
        exact = wanted in titles
        prefix = any(title.startswith(wanted) for title in titles)
        year_ok = year is None or (
            year_of(result) is not None and abs(year_of(result) - year) <= 1
        )
        # Popularity and vote count separate "Interstellar" from obscure exact-word matches.
        weight = math.log1p(result.get("popularity") or 0) + 0.5 * math.log1p(
            result.get("vote_count") or 0
        )
        return (not year_ok, not exact, -(weight + (3 if prefix else 0)))

    results.sort(key=rank)
    items = cards(results + people_works)
    if kind in ("movie", "tv"):
        items = [i for i in items if i["type"] == kind]
    return items


@web.route("/lang/<code>")
def set_language(code: str):
    if code not in LANGUAGES:
        abort(404)
    target = request.args.get("next") or request.referrer or url_for("web.home")
    parsed = urlparse(target)
    if parsed.scheme in ("http", "https") and parsed.netloc == request.host:
        target = parsed.path + (
            f"?{parsed.query}" if parsed.query else ""
        )  # the referrer
    # Only a path inside this app: no open redirect via //evil.example, /\evil.example
    # or a scheme without a host ("https:evil.example", which browsers complete).
    if not target.startswith("/") or target.startswith(("//", "/\\")):
        target = url_for("web.home")
    response = make_response(redirect(target))
    response.set_cookie(COOKIE, code, max_age=365 * 86400, samesite="Lax")
    return response


@web.route("/")
def home():
    tmdb = services().tmdb
    data = tmdb.many({key: (path, params) for key, _, path, params in HOME_ROWS})
    rows = []
    for key, label, path, _ in HOME_ROWS:
        media_type = (
            None
            if path.startswith("/trending")
            else ("tv" if "/tv" in path else "movie")
        )
        items = cards(data[key].get("results", []), media_type)
        if items:
            rows.append({"key": key, "title": t(label), "items": items})

    billboard = None
    for result in data["trending"].get("results", []):
        title = result.get("title") or result.get("name") or ""
        if (
            result.get("backdrop_path")
            and result.get("poster_path")
            and result.get("overview")
            and result.get("media_type") in ("movie", "tv")
            and not _NON_LATIN_RE.search(title)  # the big hero title must be readable
        ):
            billboard = {
                **card(result),
                "overview": result["overview"],
                "backdrop": image_url(result["backdrop_path"], "original"),
                "backdrop_srcset": backdrop_srcset(result["backdrop_path"]),
            }
            break
    return render_template("home.html", billboard=billboard, rows=rows)


@web.route("/movies", defaults={"media_type": "movie"}, endpoint="movies")
@web.route("/shows", defaults={"media_type": "tv"}, endpoint="shows")
def browse(media_type: str):
    filters = browse_filters(request.args)
    page = browse_page(media_type, filters, 1)
    return render_template(
        "browse.html",
        media_type=media_type,
        genres=services().tmdb.genres(media_type),
        filters=filters,
        sorts=BROWSE_SORTS,
        imdb_filters=IMDB_FILTERS,
        decades=BROWSE_DECADES,
        languages=BROWSE_LANGUAGES,
        imdb_ready=services().imdb.ready,
        items=page["items"],
        next_page=page["next_page"],
    )


@web.route("/search")
def search():
    query = request.args.get("q", "").strip()
    kind = (
        request.args.get("type")
        if request.args.get("type") in ("movie", "tv")
        else None
    )
    items = search_items(query, kind) if query else []
    return render_template("search.html", q=query, kind=kind, items=items)


def _detail_context(media_type: str, tmdb_id: int) -> dict[str, Any]:
    svc = services()
    data = svc.tmdb.details(media_type, tmdb_id)
    title = data.get("title") or data.get("name") or ""
    runtime = data.get("runtime") or (data.get("episode_run_time") or [None])[0]
    original = data.get("original_title") or data.get("original_name")
    imdb = svc.imdb.get(data.get("imdb_id"))
    csfd_query = data.get("english_title") or original or title
    if year_of(data):
        csfd_query = f"{csfd_query} {year_of(data)}"
    return {
        "media_type": media_type,
        "tmdb_id": tmdb_id,
        "title": title,
        "original_title": original if original and original != title else None,
        "year": year_of(data),
        "overview": data.get("overview"),
        "tagline": data.get("tagline"),
        "rating": round(data.get("vote_average") or 0, 1),
        "imdb_rating": imdb[0] if imdb else None,
        "imdb_votes": imdb[1] if imdb else None,
        "runtime": runtime,
        "runtime_label": t("hours_minutes", h=runtime // 60, m=runtime % 60)
        if media_type == "movie" and runtime
        else None,
        "genres": [g["name"] for g in data.get("genres", [])][:3],
        "poster": image_url(data.get("poster_path"), "w500"),
        "backdrop": image_url(data.get("backdrop_path"), "original"),
        "backdrop_srcset": backdrop_srcset(data.get("backdrop_path")),
        "trailer": trailer_url(data),
        "imdb_url": f"https://www.imdb.com/title/{data['imdb_id']}/"
        if data.get("imdb_id")
        else None,
        "csfd_url": f"https://www.csfd.cz/hledat/?q={quote_plus(csfd_query)}",
        "cast": [
            {
                "name": c.get("name"),
                "character": c.get("character"),
                "photo": image_url(c.get("profile_path"), "w185"),
            }
            for c in (data.get("credits") or {}).get("cast", [])[:10]
        ],
        "similar": cards(
            (data.get("recommendations") or {}).get("results", [])[:18], media_type
        ),
        "release_date": data.get("release_date") or data.get("first_air_date"),
        "released": is_released(data),
        "raw": data,
    }


def collection_row(data: dict[str, Any], tmdb_id: int) -> dict[str, Any] | None:
    """The film series a movie belongs to, oldest part first, the movie itself marked."""
    info = data.get("belongs_to_collection") or {}
    if not info.get("id"):
        return None
    try:
        collection = services().tmdb.collection(info["id"])
    except TMDBError as e:
        logger.info("%s", e)
        return None
    parts = sorted(
        collection.get("parts") or [],
        key=lambda p: (not p.get("release_date"), p.get("release_date") or ""),
    )
    items = cards(parts, "movie")
    if len(items) < 2:
        return None
    for item in items:
        item["current"] = item["id"] == tmdb_id
    return {"title": collection.get("name") or info.get("name") or "", "items": items}


@web.route("/movie/<int:tmdb_id>")
def movie(tmdb_id: int):
    context = _detail_context("movie", tmdb_id)
    collection = collection_row(context["raw"], tmdb_id)
    player = {
        "media_type": "movie",
        "tmdb_id": tmdb_id,
        "title": context["title"],
        "year": context["year"],
        "poster": context["poster"],
        "backdrop": image_url(context["raw"].get("backdrop_path"), "w780"),
        "runtime": context["runtime"],
        "release_date": context["release_date"],
        "released": context["released"],
        "convert": services().transcoder.available,
    }
    return render_template(
        "detail.html", player=player, collection=collection, **context
    )


@web.route("/show/<int:tmdb_id>")
def show(tmdb_id: int):
    context = _detail_context("tv", tmdb_id)
    raw = context["raw"]
    seasons = [
        {
            "number": s["season_number"],
            "name": s.get("name") or t("season_n", n=s["season_number"]),
            "episodes": s.get("episode_count") or 0,
        }
        for s in raw.get("seasons") or []
        if s.get("season_number") is not None and (s.get("episode_count") or 0) > 0
    ]
    seasons.sort(key=lambda s: (s["number"] == 0, s["number"]))  # specials last
    # ?s=2&e=1 (an "Up next" card) opens the season of that episode.
    requested = request.args.get("season", type=int) or request.args.get("s", type=int)
    current = (
        requested
        if any(s["number"] == requested for s in seasons)
        else (seasons[0]["number"] if seasons else None)
    )
    numbered = sum(1 for s in seasons if s["number"] > 0)
    player = {
        "media_type": "tv",
        "tmdb_id": tmdb_id,
        "title": context["title"],
        "year": context["year"],
        "poster": context["poster"],
        "backdrop": image_url(raw.get("backdrop_path"), "w780"),
        "runtime": context["runtime"],
        "seasons": seasons,
        "season": current,
        "episodes": season_episodes(tmdb_id, current) if current is not None else [],
        "convert": services().transcoder.available,
    }
    return render_template(
        "detail.html",
        player=player,
        seasons=seasons,
        current_season=current,
        seasons_label=plural(numbered, "seasons") if numbered else None,
        **context,
    )


def season_episodes(tmdb_id: int, season_number: int) -> list[dict[str, Any]]:
    season = services().tmdb.season(tmdb_id, season_number)
    return [
        {
            "season": season_number,
            "episode": e.get("episode_number"),
            "name": e.get("name") or t("episode_n", n=e.get("episode_number")),
            "overview": e.get("overview") or "",
            "runtime": e.get("runtime"),
            "air_date": e.get("air_date"),
            "still": image_url(e.get("still_path"), "w300"),
        }
        for e in season.get("episodes", [])
        if e.get("episode_number") is not None
    ]


@web.route("/settings")
def settings():
    return render_template(
        "settings.html",
        all_languages=LANGUAGE_NAMES,
        ui_languages=LANGUAGES,
        setup=bool(request.args.get("setup")),
        is_admin=auth.is_admin(),
        default_password=auth.using_default(),
        admin_username=auth.username(),
        ai_available=services().transcriber.available,
        ai_translate=services().transcriber.chat_available,
    )


@web.route("/status")
def status():
    svc = services()
    return render_template(
        "status.html",
        torrserver_online=svc.torrserver.is_online(),
        torrserver_url=svc.torrserver.base_url,
        providers=svc.torrents.provider_status(),
        subtitles=svc.subtitles.status(),
        imdb_ready=svc.imdb.ready,
        imdb_count=svc.imdb.count if svc.imdb.ready else 0,
        is_admin=auth.is_admin(),
    )


# -- old URLs, kept so bookmarks keep working ----------------------------------

# Czech paths used before the URLs were switched to English.
_RENAMED = {
    "/filmy": "/movies",
    "/serialy": "/shows",
    "/hledat": "/search",
    "/nastaveni": "/settings",
    "/stav": "/status",
}


def _moved(path: str):
    """301 to the new path, keeping the query string (e.g. ?play=1, ?q=...)."""
    query = request.query_string.decode()
    return redirect(f"{path}?{query}" if query else path, 301)


for _old, _new in _RENAMED.items():
    web.add_url_rule(
        _old, f"legacy{_old.replace('/', '_')}", lambda _new=_new: _moved(_new)
    )


@web.route("/film/<int:tmdb_id>")
def legacy_movie(tmdb_id: int):
    return _moved(f"/movie/{tmdb_id}")


@web.route("/serial/<int:tmdb_id>")
def legacy_show(tmdb_id: int):
    return _moved(f"/show/{tmdb_id}")


@web.route("/title/<media_type>/<int:tmdb_id>")
def legacy_title(media_type: str, tmdb_id: int):
    if media_type not in MEDIA_SLUG:
        abort(404)
    return redirect(detail_url(media_type, tmdb_id), 301)


@web.route("/season/<int:tmdb_id>/<int:season_number>")
def legacy_season(tmdb_id: int, season_number: int):
    return redirect(url_for("web.show", tmdb_id=tmdb_id, season=season_number), 301)


@web.route("/episode/<int:tmdb_id>/<int:season>/<int:episode>")
def legacy_episode(tmdb_id: int, season: int, episode: int):
    return redirect(
        url_for("web.show", tmdb_id=tmdb_id, season=season, _anchor=f"e{episode}"), 301
    )


@web.route("/episode-torrents")
def legacy_episode_torrents():
    tmdb_id, season = (
        request.args.get("tmdb_id", type=int),
        request.args.get("season", type=int),
    )
    if tmdb_id is None or season is None:
        abort(404)
    return redirect(url_for("web.show", tmdb_id=tmdb_id, season=season), 301)
