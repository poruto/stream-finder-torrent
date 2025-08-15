"""TMDB API client for movie and TV show data retrieval."""

from typing import Optional, Dict, Any, List, Union
from functools import lru_cache
from dataclasses import dataclass
import requests

from config import TMDB_API_KEY, LANGUAGE


@dataclass
class TMDBError(Exception):
    """Custom exception for TMDB API errors."""
    message: str
    status_code: Optional[int] = None


class TMDBClient:
    """Client for interacting with TMDB API."""

    BASE_URL = "https://api.themoviedb.org/3"
    IMAGE_BASE_URL = "https://image.tmdb.org/t/p"
    DEFAULT_TIMEOUT = 20

    def __init__(self, api_key: str, language: str = "en-US"):
        if not api_key:
            raise TMDBError("TMDB API key is required")

        self.api_key = api_key
        self.language = language
        self.session = requests.Session()
        self.session.params.update({
            "api_key": self.api_key,
            "language": self.language
        })

    def _make_request(self, path: str, language_override: str = None, **params) -> Dict[str, Any]:
        """Make authenticated request to TMDB API."""
        url = f"{self.BASE_URL}{path}"

        # Override language if specified
        request_params = params.copy()
        if language_override:
            request_params["language"] = language_override

        try:
            response = self.session.get(url, params=request_params, timeout=self.DEFAULT_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.Timeout:
            raise TMDBError(f"Request timeout for {path}")
        except requests.exceptions.RequestException as e:
            raise TMDBError(f"Request failed: {str(e)}", getattr(e.response, 'status_code', None))

    def _get_with_english_fallback(self, path: str, **params) -> Dict[str, Any]:
        """Get data with English title fallback for torrent searching."""
        # Get data in user's language
        data = self._make_request(path, **params)

        # Get English data for better torrent search results
        try:
            english_data = self._make_request(path, language_override="en-US", **params)

            # Add English titles
            if "title" in english_data:
                data["original_title"] = english_data.get("original_title") or english_data.get("title", "")
            if "name" in english_data:
                data["original_name"] = english_data.get("original_name") or english_data.get("name", "")
        except TMDBError:
            pass  # Continue without English data if request fails

        return data


class TMDBSearchMixin:
    """Mixin for search-related functionality."""

    def search_multi(self, query: str, page: int = 1) -> Dict[str, Any]:
        """Search for movies, TV shows and people."""
        return self._make_request("/search/multi", query=query, page=page)

    def discover_movies(self, genre_ids: List[int] = None, min_rating: float = 0,
                        max_rating: float = 10, sort_by: str = "popularity.desc",
                        year: int = None, page: int = 1) -> Dict[str, Any]:
        """Advanced movie search by genres and ratings."""
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

        return self._make_request("/discover/movie", **params)

    def discover_tv(self, genre_ids: List[int] = None, min_rating: float = 0,
                    max_rating: float = 10, sort_by: str = "popularity.desc",
                    year: int = None, page: int = 1) -> Dict[str, Any]:
        """Advanced TV show search by genres and ratings."""
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

        return self._make_request("/discover/tv", **params)


class TMDBContentMixin:
    """Mixin for content retrieval functionality."""

    def get_movie(self, tmdb_id: int) -> Dict[str, Any]:
        """Get movie details with credits and English title."""
        data = self._get_with_english_fallback(f"/movie/{tmdb_id}")
        credits = self._make_request(f"/movie/{tmdb_id}/credits")
        data["credits"] = credits
        return data

    def get_tv(self, tmdb_id: int) -> Dict[str, Any]:
        """Get TV show details with credits and English title."""
        data = self._get_with_english_fallback(f"/tv/{tmdb_id}")
        credits = self._make_request(f"/tv/{tmdb_id}/credits")
        data["credits"] = credits
        return data

    def get_tv_season(self, tmdb_id: int, season_number: int) -> Dict[str, Any]:
        """Get TV season details."""
        return self._make_request(f"/tv/{tmdb_id}/season/{season_number}")

    def get_tv_episode(self, tmdb_id: int, season_number: int, episode_number: int) -> Dict[str, Any]:
        """Get TV episode details."""
        return self._make_request(f"/tv/{tmdb_id}/season/{season_number}/episode/{episode_number}")


class TMDBTrendingMixin:
    """Mixin for trending and popular content."""

    def get_trending(self, media_type: str = "all", time_window: str = "week", page: int = 1) -> Dict[str, Any]:
        """Get trending content (all/movie/tv, day/week)."""
        if media_type not in ["all", "movie", "tv"]:
            raise TMDBError(f"Invalid media_type: {media_type}")
        if time_window not in ["day", "week"]:
            raise TMDBError(f"Invalid time_window: {time_window}")

        return self._make_request(f"/trending/{media_type}/{time_window}", page=page)

    def get_popular_movies(self, page: int = 1) -> Dict[str, Any]:
        """Get popular movies."""
        return self._make_request("/movie/popular", page=page)

    def get_popular_tv(self, page: int = 1) -> Dict[str, Any]:
        """Get popular TV shows."""
        return self._make_request("/tv/popular", page=page)

    def get_top_rated_movies(self, page: int = 1) -> Dict[str, Any]:
        """Get top rated movies."""
        return self._make_request("/movie/top_rated", page=page)

    def get_top_rated_tv(self, page: int = 1) -> Dict[str, Any]:
        """Get top rated TV shows."""
        return self._make_request("/tv/top_rated", page=page)

    def get_now_playing_movies(self, page: int = 1) -> Dict[str, Any]:
        """Get currently playing movies."""
        return self._make_request("/movie/now_playing", page=page)

    def get_upcoming_movies(self, page: int = 1) -> Dict[str, Any]:
        """Get upcoming movies."""
        return self._make_request("/movie/upcoming", page=page)


class TMDBGenreMixin:
    """Mixin for genre-related functionality."""

    @lru_cache(maxsize=2)
    def get_movie_genres(self) -> List[Dict[str, Any]]:
        """Get list of movie genres (cached)."""
        data = self._make_request("/genre/movie/list")
        return data.get("genres", [])

    @lru_cache(maxsize=2)
    def get_tv_genres(self) -> List[Dict[str, Any]]:
        """Get list of TV genres (cached)."""
        data = self._make_request("/genre/tv/list")
        return data.get("genres", [])

    def get_genre_name(self, genre_id: int, media_type: str = "movie") -> str:
        """Get genre name by ID."""
        try:
            genres = self.get_movie_genres() if media_type == "movie" else self.get_tv_genres()
            for genre in genres:
                if genre["id"] == genre_id:
                    return genre["name"]
            return "Unknown"
        except TMDBError:
            return "Unknown"


class TMDB(TMDBClient, TMDBSearchMixin, TMDBContentMixin, TMDBTrendingMixin, TMDBGenreMixin):
    """Main TMDB API client with all functionality."""

    def get_english_title(self, tmdb_id: int, media_type: str) -> str:
        """Get English title for torrent searching."""
        try:
            if media_type == "movie":
                data = self._make_request(f"/movie/{tmdb_id}", language_override="en-US")
                return data.get("title", "")
            else:  # tv
                data = self._make_request(f"/tv/{tmdb_id}", language_override="en-US")
                return data.get("name", "")
        except TMDBError:
            return ""

    def get_imdb_rating(self, tmdb_id: int, media_type: str) -> Optional[float]:
        """Get IMDB rating."""
        try:
            if media_type == "movie":
                data = self._make_request(f"/movie/{tmdb_id}")
            else:
                data = self._make_request(f"/tv/{tmdb_id}")
            return data.get("vote_average")
        except TMDBError:
            return None

    def imdb_url_from_movie(self, tmdb_id: int) -> Optional[str]:
        """Get IMDB URL for movie."""
        try:
            data = self._make_request(f"/movie/{tmdb_id}")
            imdb_id = data.get("imdb_id")
            return f"https://www.imdb.com/title/{imdb_id}" if imdb_id else None
        except TMDBError:
            return None

    def poster_url(self, path: Optional[str], size: str = "w500") -> Optional[str]:
        """Generate poster URL from path."""
        if not path:
            return None
        return f"{self.IMAGE_BASE_URL}/{size}{path}"


class TMDBUtils:
    """Utility functions for TMDB data."""

    @staticmethod
    def format_rating(rating: Optional[float]) -> str:
        """Format rating for display."""
        if rating:
            return f"⭐ {rating:.1f}/10"
        return "❓ Bez hodnocení"

    @staticmethod
    def extract_year(date_string: str) -> str:
        """Extract year from date string."""
        return date_string[:4] if date_string else ""

    @staticmethod
    def get_title(item: Dict[str, Any]) -> str:
        """Get title from movie or TV show data."""
        return item.get('title') or item.get('name', 'Unknown Title')

    @staticmethod
    def get_original_title(item: Dict[str, Any]) -> str:
        """Get original title from movie or TV show data."""
        return item.get('original_title') or item.get('original_name', '')

    @staticmethod
    def get_release_date(item: Dict[str, Any]) -> str:
        """Get release date from movie or TV show data."""
        return item.get('release_date') or item.get('first_air_date', '')


# Initialize global client
tmdb_client = TMDB(TMDB_API_KEY, LANGUAGE)


# Export functions for backward compatibility
def search_multi(query: str, page: int = 1) -> Dict[str, Any]:
    return tmdb_client.search_multi(query, page)


def discover_movies(genre_ids: List[int] = None, min_rating: float = 0,
                    max_rating: float = 10, sort_by: str = "popularity.desc",
                    year: int = None, page: int = 1) -> Dict[str, Any]:
    return tmdb_client.discover_movies(genre_ids, min_rating, max_rating, sort_by, year, page)


def discover_tv(genre_ids: List[int] = None, min_rating: float = 0,
                max_rating: float = 10, sort_by: str = "popularity.desc",
                year: int = None, page: int = 1) -> Dict[str, Any]:
    return tmdb_client.discover_tv(genre_ids, min_rating, max_rating, sort_by, year, page)


def get_movie_genres() -> List[Dict[str, Any]]:
    return tmdb_client.get_movie_genres()


def get_tv_genres() -> List[Dict[str, Any]]:
    return tmdb_client.get_tv_genres()


def get_trending(media_type: str = "all", time_window: str = "week", page: int = 1) -> Dict[str, Any]:
    return tmdb_client.get_trending(media_type, time_window, page)


def get_popular_movies(page: int = 1) -> Dict[str, Any]:
    return tmdb_client.get_popular_movies(page)


def get_popular_tv(page: int = 1) -> Dict[str, Any]:
    return tmdb_client.get_popular_tv(page)


def get_top_rated_movies(page: int = 1) -> Dict[str, Any]:
    return tmdb_client.get_top_rated_movies(page)


def get_top_rated_tv(page: int = 1) -> Dict[str, Any]:
    return tmdb_client.get_top_rated_tv(page)


def get_now_playing_movies(page: int = 1) -> Dict[str, Any]:
    return tmdb_client.get_now_playing_movies(page)


def get_upcoming_movies(page: int = 1) -> Dict[str, Any]:
    return tmdb_client.get_upcoming_movies(page)


def get_movie(tmdb_id: int) -> Dict[str, Any]:
    return tmdb_client.get_movie(tmdb_id)


def get_tv(tmdb_id: int) -> Dict[str, Any]:
    return tmdb_client.get_tv(tmdb_id)


def get_tv_season(tmdb_id: int, season_number: int) -> Dict[str, Any]:
    return tmdb_client.get_tv_season(tmdb_id, season_number)


def get_tv_episode(tmdb_id: int, season_number: int, episode_number: int) -> Dict[str, Any]:
    return tmdb_client.get_tv_episode(tmdb_id, season_number, episode_number)


def get_english_title(tmdb_id: int, media_type: str) -> str:
    return tmdb_client.get_english_title(tmdb_id, media_type)


def tmdb_poster(path: Optional[str], size: str = "w500") -> Optional[str]:
    return tmdb_client.poster_url(path, size)


def imdb_url_from_tmdb_movie(tmdb_id: int) -> Optional[str]:
    return tmdb_client.imdb_url_from_movie(tmdb_id)


def get_imdb_rating(tmdb_id: int, media_type: str) -> Optional[float]:
    return tmdb_client.get_imdb_rating(tmdb_id, media_type)


def format_rating(rating: Optional[float]) -> str:
    return TMDBUtils.format_rating(rating)


def get_genre_name(genre_id: int, media_type: str = "movie") -> str:
    return tmdb_client.get_genre_name(genre_id, media_type)

def tmdb_profile_image(profile_path: str, size: str = "w185") -> str:
    """Generate TMDB profile image URL."""
    if not profile_path:
        return ""
    return f"https://image.tmdb.org/t/p/{size}{profile_path}"