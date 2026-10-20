"""Calendar: what comes out in the next weeks for the titles the household follows.

Shows being watched or kept in My list give their upcoming episodes; movies in My list
give their cinema premiere and - more useful for torrents - their digital release
(TMDB release type 4: from then on there are good versions to play).
"""

import datetime
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from flask import Blueprint, jsonify, render_template, request

from .services import services
from .tmdb import TMDBError, image_url
from .views import detail_url

upcoming_bp = Blueprint("upcoming", __name__)

DAYS_AHEAD = 60
DAY_TTL = 12 * 3600
DIGITAL, PHYSICAL, THEATRICAL = 4, 5, 3


def _ids(name: str) -> list[int]:
    return [int(i) for i in request.args.get(name, "").split(",") if i.isdigit()][:40]


def show_events(tmdb, tmdb_id: int, today: str, until: str) -> list[dict[str, Any]]:
    """The show's episodes airing between `today` and `until` (specials left out)."""
    show = tmdb._get(f"/tv/{tmdb_id}", ttl=DAY_TTL)
    upcoming = show.get("next_episode_to_air") or {}
    season = upcoming.get("season_number")
    if not season:
        return []
    episodes = (
        tmdb._get(f"/tv/{tmdb_id}/season/{season}", ttl=DAY_TTL).get("episodes") or []
    )
    base = {
        "type": "tv",
        "id": tmdb_id,
        "title": show.get("name") or "",
        "poster": image_url(show.get("poster_path"), "w185"),
    }
    return [
        {
            **base,
            "kind": "episode",
            "date": ep["air_date"],
            "season": season,
            "episode": ep.get("episode_number"),
            "name": ep.get("name") or "",
            "premiere": ep.get("episode_number") == 1,
        }
        for ep in episodes
        if ep.get("air_date") and today <= ep["air_date"] <= until
    ]


def movie_events(tmdb, tmdb_id: int, today: str, until: str) -> list[dict[str, Any]]:
    """Cinema premiere and the first digital release, when they are still ahead."""
    movie = tmdb._get(
        f"/movie/{tmdb_id}", ttl=DAY_TTL, append_to_response="release_dates"
    )
    dates: dict[int, str] = {}
    for country in (movie.get("release_dates") or {}).get("results") or []:
        for release in country.get("release_dates") or []:
            kind, when = release.get("type"), (release.get("release_date") or "")[:10]
            if kind in (DIGITAL, PHYSICAL, THEATRICAL) and when:
                dates[kind] = min(dates.get(kind, when), when)
    if movie.get("release_date"):
        dates.setdefault(THEATRICAL, movie["release_date"])
    base = {
        "type": "movie",
        "id": tmdb_id,
        "title": movie.get("title") or "",
        "poster": image_url(movie.get("poster_path"), "w185"),
    }
    events = []
    digital = dates.get(DIGITAL) or dates.get(PHYSICAL)
    if dates.get(THEATRICAL) and today <= dates[THEATRICAL] <= until:
        events.append({**base, "kind": "cinema", "date": dates[THEATRICAL]})
    if digital and today <= digital <= until:
        events.append({**base, "kind": "digital", "date": digital})
    return events


@upcoming_bp.route("/calendar")
def page():
    """The Calendar tab; filled in the browser from the household's progress and My list."""
    return render_template("calendar.html", days=DAYS_AHEAD)


@upcoming_bp.route("/api/calendar")
def upcoming():
    """?shows=1,2&movies=3,4 (TMDB ids): their releases in the next DAYS_AHEAD days."""
    tmdb = services().tmdb
    start = datetime.date.today()
    today, until = (
        start.isoformat(),
        (start + datetime.timedelta(days=DAYS_AHEAD)).isoformat(),
    )
    jobs = [(show_events, i) for i in _ids("shows")] + [
        (movie_events, i) for i in _ids("movies")
    ]

    def run(job):
        fetch, tmdb_id = job
        try:
            return fetch(tmdb, tmdb_id, today, until)
        except TMDBError:
            return []

    with ThreadPoolExecutor(max_workers=6) as pool:
        events = [event for found in pool.map(run, jobs) for event in found]
    for event in events:  # url_for needs this request's thread, not the pool's
        event["url"] = detail_url(event["type"], event["id"])
    events.sort(
        key=lambda e: (
            e["date"],
            e["title"],
            e.get("season") or 0,
            e.get("episode") or 0,
        )
    )
    return jsonify(success=True, today=today, events=events)
