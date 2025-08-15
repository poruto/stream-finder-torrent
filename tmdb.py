import requests
from typing import Optional, Dict, Any, List
from config import TMDB_API_KEY, LANGUAGE

TMDB_BASE = "https://api.themoviedb.org/3"


def _get(path: str, **params):
    if not TMDB_API_KEY:
        raise RuntimeError("Chybí TMDB_API_KEY v .env")
    p = {"api_key": TMDB_API_KEY, "language": LANGUAGE}
    p.update(params)
    r = requests.get(f"{TMDB_BASE}{path}", params=p, timeout=20)
    r.raise_for_status()
    return r.json()


def _get_english(path: str, **params):
    """Získá data v angličtině pro vyhledávání torrentů"""
    if not TMDB_API_KEY:
        raise RuntimeError("Chybí TMDB_API_KEY v .env")
    p = {"api_key": TMDB_API_KEY, "language": "en-US"}
    p.update(params)
    r = requests.get(f"{TMDB_BASE}{path}", params=p, timeout=20)
    r.raise_for_status()
    return r.json()


def search_multi(query: str, page: int = 1) -> Dict[str, Any]:
    data = _get("/search/multi", query=query, page=page)
    return data


def discover_movies(genre_ids: List[int] = None, min_rating: float = 0,
                    max_rating: float = 10, sort_by: str = "popularity.desc",
                    year: int = None, page: int = 1) -> Dict[str, Any]:
    """Pokročilé vyhledávání filmů podle žánrů a hodnocení"""
    params = {
        'sort_by': sort_by,
        'page': page,
        'vote_average.gte': min_rating,
        'vote_average.lte': max_rating
    }

    if genre_ids:
        params['with_genres'] = ','.join(map(str, genre_ids))

    if year:
        params['year'] = year

    return _get("/discover/movie", **params)


def discover_tv(genre_ids: List[int] = None, min_rating: float = 0,
                max_rating: float = 10, sort_by: str = "popularity.desc",
                year: int = None, page: int = 1) -> Dict[str, Any]:
    """Pokročilé vyhledávání seriálů podle žánrů a hodnocení"""
    params = {
        'sort_by': sort_by,
        'page': page,
        'vote_average.gte': min_rating,
        'vote_average.lte': max_rating
    }

    if genre_ids:
        params['with_genres'] = ','.join(map(str, genre_ids))

    if year:
        params['first_air_date_year'] = year

    return _get("/discover/tv", **params)


def get_movie_genres() -> List[Dict[str, Any]]:
    """Získá seznam žánrů pro filmy"""
    data = _get("/genre/movie/list")
    return data.get("genres", [])


def get_tv_genres() -> List[Dict[str, Any]]:
    """Získá seznam žánrů pro seriály"""
    data = _get("/genre/tv/list")
    return data.get("genres", [])


def get_trending(media_type: str = "all", time_window: str = "week", page: int = 1) -> Dict[str, Any]:
    """Získá trendy - all/movie/tv, day/week"""
    return _get(f"/trending/{media_type}/{time_window}", page=page)


def get_popular_movies(page: int = 1) -> Dict[str, Any]:
    """Získá populární filmy"""
    return _get("/movie/popular", page=page)


def get_popular_tv(page: int = 1) -> Dict[str, Any]:
    """Získá populární seriály"""
    return _get("/tv/popular", page=page)


def get_top_rated_movies(page: int = 1) -> Dict[str, Any]:
    """Získá nejlépe hodnocené filmy"""
    return _get("/movie/top_rated", page=page)


def get_top_rated_tv(page: int = 1) -> Dict[str, Any]:
    """Získá nejlépe hodnocené seriály"""
    return _get("/tv/top_rated", page=page)


def get_now_playing_movies(page: int = 1) -> Dict[str, Any]:
    """Získá aktuálně promítané filmy"""
    return _get("/movie/now_playing", page=page)


def get_upcoming_movies(page: int = 1) -> Dict[str, Any]:
    """Získá připravované filmy"""
    return _get("/movie/upcoming", page=page)


def get_movie(tmdb_id: int) -> Dict[str, Any]:
    data = _get(f"/movie/{tmdb_id}")
    credits = _get(f"/movie/{tmdb_id}/credits")
    data["credits"] = credits

    # Přidáme anglický název pro vyhledávání torrentů
    english_data = _get_english(f"/movie/{tmdb_id}")
    original_title = english_data.get("original_title", "")
    if not original_title:
        original_title = english_data.get("title", data.get("original_title", ""))
    data["original_title"] = original_title

    return data


def get_tv(tmdb_id: int) -> Dict[str, Any]:
    data = _get(f"/tv/{tmdb_id}")
    credits = _get(f"/tv/{tmdb_id}/credits")
    data["credits"] = credits

    # Přidáme anglický název pro vyhledávání torrentů
    english_data = _get_english(f"/tv/{tmdb_id}")
    original_name = english_data.get("original_name", "")
    if not original_name:
        original_name = english_data.get("name", data.get("original_name", ""))
    data["original_name"] = original_name

    return data


def get_tv_season(tmdb_id: int, season_number: int) -> Dict[str, Any]:
    """Získá detaily konkrétní sezóny seriálu"""
    return _get(f"/tv/{tmdb_id}/season/{season_number}")


def get_tv_episode(tmdb_id: int, season_number: int, episode_number: int) -> Dict[str, Any]:
    """Získá detaily konkrétní epizody"""
    return _get(f"/tv/{tmdb_id}/season/{season_number}/episode/{episode_number}")


def get_english_title(tmdb_id: int, media_type: str) -> str:
    """Získá anglický název pro vyhledávání torrentů"""
    try:
        if media_type == "movie":
            data = _get_english(f"/movie/{tmdb_id}")
            return data.get("title", "")
        else:  # tv
            data = _get_english(f"/tv/{tmdb_id}")
            return data.get("name", "")
    except:
        return ""


def tmdb_poster(path: Optional[str], size: str = "w500") -> Optional[str]:
    if not path:
        return None
    return f"https://image.tmdb.org/t/p/{size}{path}"


def imdb_url_from_tmdb_movie(tmdb_id: int) -> Optional[str]:
    data = _get(f"/movie/{tmdb_id}")
    imdb_id = data.get("imdb_id")
    return f"https://www.imdb.com/title/{imdb_id}" if imdb_id else None


def get_imdb_rating(tmdb_id: int, media_type: str) -> Optional[float]:
    """Získá IMDB hodnocení"""
    try:
        if media_type == "movie":
            data = _get(f"/movie/{tmdb_id}")
        else:
            data = _get(f"/tv/{tmdb_id}")
        return data.get("vote_average")
    except:
        return None


def format_rating(rating: float) -> str:
    """Formátuje hodnocení pro zobrazení"""
    if rating:
        return f"⭐ {rating:.1f}/10"
    return "❓ Bez hodnocení"


def get_genre_name(genre_id: int, media_type: str = "movie") -> str:
    """Získá název žánru podle ID"""
    try:
        if media_type == "movie":
            genres = get_movie_genres()
        else:
            genres = get_tv_genres()

        for genre in genres:
            if genre["id"] == genre_id:
                return genre["name"]
        return "Neznámý"
    except:
        return "Neznámý"