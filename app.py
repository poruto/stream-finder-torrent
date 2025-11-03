"""Flask application for streaming movies and TV shows via TorrServer."""

import re
import binascii
from typing import Dict, List, Optional, Any, Union

import requests
from flask import Flask, render_template, request, jsonify, send_file, abort

from tmdb import (
    search_multi, get_movie, get_tv, get_tv_season, tmdb_poster,
    imdb_url_from_tmdb_movie, discover_movies, discover_tv,
    get_movie_genres, get_tv_genres, get_trending, get_popular_movies,
    get_popular_tv, get_top_rated_movies, get_top_rated_tv,
    get_now_playing_movies, get_upcoming_movies, format_rating, tmdb_profile_image, tmdb_client
)
from torrent_search import TorrentSearcher
from config import TORRSERVER_URL, TORRSERVER_STREAM_PATH
from subtitle_manager import SubtitleManager

app = Flask(__name__)
torrent_searcher = TorrentSearcher()
subtitle_manager = SubtitleManager()

MAGNET_RE = re.compile(r'magnet:\?xt=urn:btih:([a-fA-F0-9]{40}|[a-fA-F0-9]{32})')
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v'}


class TorrentManager:
    """Handles torrent operations with TorrServer."""

    def __init__(self, base_url: str, stream_path: str):
        self.base_url = base_url.rstrip('/')
        self.stream_path = stream_path

    def add_torrent(self, magnet_link: str) -> Dict[str, Any]:
        """Add torrent to TorrServer and return stream URL."""
        if not magnet_link.startswith('magnet:'):
            return {"success": False, "error": "Invalid magnet link"}

        hash_string = self._extract_hash(magnet_link)
        if not hash_string:
            return {"success": False, "error": "Cannot extract hash from magnet link"}

        # Check if torrent already exists
        existing_result = self._check_existing_torrent(hash_string)
        if existing_result:
            return existing_result

        # Add new torrent
        return self._add_new_torrent(magnet_link, hash_string)

    def _extract_hash(self, magnet_link: str) -> Optional[str]:
        """Extract hash from magnet link."""
        match = MAGNET_RE.search(magnet_link)
        if not match:
            return None

        hash_string = match.group(1)

        # Convert 32-hex to 40-hex if needed
        if len(hash_string) == 32:
            hash_string = self._convert_hash_format(hash_string)

        return hash_string

    def _convert_hash_format(self, hash_string: str) -> str:
        """Convert hash from 32-char to 40-char format."""
        try:
            import base64
            decoded = base64.b32decode(hash_string + '=' * (8 - len(hash_string) % 8))
            return decoded.hex()
        except Exception:
            try:
                hash_bytes = binascii.unhexlify(hash_string)
                return binascii.hexlify(hash_bytes).decode('ascii')
            except Exception:
                return hash_string

    def _check_existing_torrent(self, hash_string: str) -> Optional[Dict[str, Any]]:
        """Check if torrent already exists in TorrServer."""
        try:
            response = requests.get(f"{self.base_url}/torrents", timeout=10)
            if response.status_code != 200:
                return None

            existing_torrents = response.json()
            for torrent in existing_torrents:
                if torrent.get('hash', '').lower() == hash_string.lower():
                    stream_url = f"{self.base_url}{self.stream_path}?link={hash_string}&index=1&play"
                    return {
                        "success": True,
                        "stream_url": stream_url,
                        "hash": hash_string,
                        "message": "Torrent already exists, stream is ready"
                    }
        except requests.RequestException as e:
            print(f"Cannot check existing torrents: {e}")

        return None

    def _add_new_torrent(self, magnet_link: str, hash_string: str) -> Dict[str, Any]:
        """Add new torrent to TorrServer."""
        try:
            add_data = {
                "action": "add",
                "link": magnet_link,
                "save_to_db": True
            }

            response = requests.post(
                f"{self.base_url}/torrents",
                json=add_data,
                timeout=30
            )

            if response.status_code == 200:
                stream_url = f"{self.base_url}{self.stream_path}?link={hash_string}&index=1&play"
                return {
                    "success": True,
                    "stream_url": stream_url,
                    "hash": hash_string,
                    "message": "Torrent successfully added"
                }
            else:
                return {
                    "success": False,
                    "error": f"TorrServer error ({response.status_code}): {response.text}"
                }

        except requests.exceptions.Timeout:
            return {"success": False, "error": "TorrServer timeout - try again"}
        except requests.exceptions.ConnectionError:
            return {"success": False, "error": f"Cannot connect to TorrServer ({self.base_url})"}
        except Exception as e:
            return {"success": False, "error": f"Unexpected error: {str(e)}"}


class SearchResultProcessor:
    """Processes search results from TMDB."""

    @staticmethod
    def process_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Process TMDB search results."""
        items = []
        for result in results:
            if SearchResultProcessor._is_valid_result(result):
                item = SearchResultProcessor._create_item(result)
                items.append(item)
        return items

    @staticmethod
    def _is_valid_result(result: Dict[str, Any]) -> bool:
        """Check if result is a valid movie or TV show."""
        return (
                result.get('media_type') in ['movie', 'tv'] or
                'release_date' in result or
                'first_air_date' in result
        )

    @staticmethod
    def _create_item(result: Dict[str, Any]) -> Dict[str, Any]:
        """Create standardized item from TMDB result."""
        # Determine media type if not explicit
        if 'media_type' not in result:
            result['media_type'] = 'movie' if 'release_date' in result else 'tv'

        return {
            'tmdb_id': result['id'],
            'media_type': result['media_type'],
            'title': result.get('title') or result.get('name', 'Unknown Title'),
            'year': (result.get('release_date') or result.get('first_air_date', ''))[:4],
            'poster': tmdb_poster(result.get('poster_path')),
            'rating': result.get('vote_average', 0),
            'rating_formatted': format_rating(result.get('vote_average')),
            'genres': result.get('genre_ids', [])
        }


class SearchHandler:
    """Handles different types of content searches."""

    SEARCH_FUNCTIONS = {
        'trending': lambda params: get_trending(
            params.get('media_type', 'all'),
            params.get('time_window', 'week'),
            params.get('page', 1)
        ),
        'popular_movies': lambda params: get_popular_movies(params.get('page', 1)),
        'popular_tv': lambda params: get_popular_tv(params.get('page', 1)),
        'top_rated_movies': lambda params: get_top_rated_movies(params.get('page', 1)),
        'top_rated_tv': lambda params: get_top_rated_tv(params.get('page', 1)),
        'now_playing': lambda params: get_now_playing_movies(params.get('page', 1)),
        'upcoming': lambda params: get_upcoming_movies(params.get('page', 1)),
        'discover_movies': lambda params: discover_movies(
            [params.get('genre')] if params.get('genre') else None,
            params.get('min_rating', 0),
            params.get('max_rating', 10),
            params.get('sort_by', 'popularity.desc'),
            params.get('year'),
            params.get('page', 1)
        ),
        'discover_tv': lambda params: discover_tv(
            [params.get('genre')] if params.get('genre') else None,
            params.get('min_rating', 0),
            params.get('max_rating', 10),
            params.get('sort_by', 'popularity.desc'),
            params.get('year'),
            params.get('page', 1)
        )
    }

    def search(self, query: str, category: str, params: Dict[str, Any]) -> tuple:
        """Perform search based on category and parameters."""
        if query and category == 'search':
            results = search_multi(query, page=params.get('page', 1))
        elif category in self.SEARCH_FUNCTIONS:
            results = self.SEARCH_FUNCTIONS[category](params)
        else:
            results = {'results': [], 'total_pages': 1}

        items = SearchResultProcessor.process_results(results.get('results', []))
        total_pages = results.get('total_pages', 1)

        return items, total_pages


# Initialize managers
torrent_manager = TorrentManager(TORRSERVER_URL, TORRSERVER_STREAM_PATH)
search_handler = SearchHandler()


@app.route('/')
def index():
    """Main page with search and content discovery."""
    # Extract parameters
    query = request.args.get('q', '').strip()
    category = request.args.get('category', 'search')
    genre = request.args.get('genre', type=int)
    min_rating = request.args.get('min_rating', type=float, default=0)
    max_rating = request.args.get('max_rating', type=float, default=10)
    year = request.args.get('year', type=int)
    sort_by = request.args.get('sort_by', 'popularity.desc')
    page = request.args.get('page', type=int, default=1)

    # Prepare search parameters
    search_params = {
        'page': page,
        'genre': genre,
        'min_rating': min_rating,
        'max_rating': max_rating,
        'year': year,
        'sort_by': sort_by,
        'media_type': request.args.get('media_type', 'all'),
        'time_window': request.args.get('time_window', 'week')
    }

    # Perform search
    try:
        items, total_pages = search_handler.search(query, category, search_params)
    except Exception as e:
        print(f"Search error: {e}")
        items, total_pages = [], 1

    # Get genres for filters
    movie_genres = get_movie_genres()
    tv_genres = get_tv_genres()

    return render_template(
        'index.html',
        items=items,
        q=query,
        category=category,
        genre=genre,
        min_rating=min_rating,
        max_rating=max_rating,
        year=year,
        sort_by=sort_by,
        current_page=page,
        total_pages=min(total_pages, 500),  # TMDB limit
        movie_genres=movie_genres,
        tv_genres=tv_genres
    )


@app.route('/title/<media_type>/<int:tmdb_id>', methods=['GET', 'POST'])
def title_detail(media_type: str, tmdb_id: int):
    """Display title details and handle torrent submissions."""
    if media_type not in ['movie', 'tv']:
        return "Invalid media_type", 400

    try:
        # Get title data
        data = get_movie(tmdb_id) if media_type == 'movie' else get_tv(tmdb_id)

        # Handle magnet form submission
        if request.method == 'POST':
            magnet = request.form.get('magnet', '').strip()
            if magnet:
                result = torrent_manager.add_torrent(magnet)
                return _render_title_with_result(data, media_type, tmdb_id, result)

        # Render normal title page
        return _render_title_template(data, media_type, tmdb_id)

    except Exception as e:
        print(f"Error in title_detail: {e}")
        return f"Error: {str(e)}", 500


@app.route('/season/<int:tmdb_id>/<int:season_number>')
def season_detail(tmdb_id: int, season_number: int):
    """Display season details with episodes."""
    try:
        tv_data = get_tv(tmdb_id)
        season_data = get_tv_season(tmdb_id, season_number)

        return render_template(
            'season.html',
            title=tv_data.get('name', 'Unknown Series'),
            english_title=tv_data.get('original_name', tv_data.get('name', '')),
            season_name=season_data.get('name', f'Season {season_number}'),
            season_number=season_number,
            episodes=season_data.get('episodes', []),
            poster=tmdb_poster(season_data.get('poster_path')),
            tmdb_id=tmdb_id,
            torrserver_url=TORRSERVER_URL
        )

    except Exception as e:
        print(f"Error in season_detail: {e}")
        return f"Error: {str(e)}", 500


@app.route('/episode-torrents')
def episode_torrents():
    """Search torrents for specific episode."""
    try:
        required_params = ['title', 'season', 'episode']
        params = {}

        for param in required_params:
            value = request.args.get(param)
            if value is None:
                return "Missing parameters", 400
            params[param] = value

        params.update({
            'english_title': request.args.get('english_title', params['title']),
            'episode_name': request.args.get('episodeName', ''),
            'tmdb_id': request.args.get('tmdb_id', type=int)
        })

        # Převeď season a episode na integers pro JavaScript
        try:
            season_int = int(params['season'])
            episode_int = int(params['episode'])
        except (ValueError, TypeError):
            return "Invalid season/episode numbers", 400

        return render_template(
            'title.html',
            mode='episode_torrents',
            media_type='tv',
            season=season_int,  # Číselná hodnota
            episode=episode_int,  # Číselná hodnota
            title=params['title'],
            english_title=params['english_title'],
            episode_name=params['episode_name'],
            tmdb_id=params['tmdb_id'],
            year='',  # Prázdný rok pro epizody
            TORRSERVER_URL=TORRSERVER_URL
        )

    except Exception as e:
        print(f"Error in episode_torrents: {e}")
        return f"Error: {str(e)}", 500

@app.route('/api/torrents', methods=['POST'])
def search_torrents_api():
    """API endpoint for torrent search."""
    try:
        data = request.get_json()
        if not data or not data.get('title'):
            return jsonify([])

        torrents = torrent_searcher.search_torrents(
            title=data.get('english_title') or data.get('title'),
            year=data.get('year', ''),
            media_type=data.get('media_type', 'movie'),
            season=data.get('season'),
            episode=data.get('episode')
        )

        return jsonify(torrents)

    except Exception as e:
        print(f"Error in torrent search API: {e}")
        return jsonify([])


@app.route('/api/play-torrent', methods=['POST'])
def play_torrent_api():
    """API endpoint for adding torrent to TorrServer."""
    try:
        data = request.get_json()
        magnet = data.get('magnet', '').strip()

        if not magnet:
            return jsonify({"success": False, "error": "Missing magnet link"})

        result = torrent_manager.add_torrent(magnet)
        return jsonify(result)

    except Exception as e:
        print(f"API play-torrent error: {e}")
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/torrserver-status')
def torrserver_status():
    """Check TorrServer status."""
    try:
        response = requests.get(f"{TORRSERVER_URL.rstrip('/')}/echo", timeout=5)
        status = "online" if response.status_code == 200 else "offline"
    except Exception:
        status = "offline"

    return jsonify({"status": status, "url": TORRSERVER_URL})


@app.route('/api/tracker-status')
def tracker_status():
    """Get torrent tracker status."""
    try:
        status = torrent_searcher.get_tracker_status()
        return jsonify(status)
    except Exception as e:
        return jsonify({"error": str(e)})


# === SUBTITLE API ENDPOINTS ===

@app.route('/api/subtitles/search', methods=['POST'])
def search_subtitles_api():
    """API endpoint for subtitle search."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No data provided"})

        # Parametry pro vyhledávání
        imdb_id = data.get('imdb_id')
        tmdb_id = data.get('tmdb_id')
        query = data.get('query')
        year = data.get('year')
        season_number = data.get('season')
        episode_number = data.get('episode')
        languages = data.get('languages', subtitle_manager.languages)

        # Vyhledej titulky
        subtitles = subtitle_manager.search_subtitles(
            imdb_id=imdb_id,
            tmdb_id=tmdb_id,
            query=query,
            year=year,
            season_number=season_number,
            episode_number=episode_number,
            languages=languages
        )

        # Převeď na JSON serializovatelný formát
        subtitle_list = []
        for subtitle in subtitles:
            subtitle_list.append({
                'id': subtitle.id,
                'name': subtitle.name,
                'language': subtitle.language,
                'language_code': subtitle.language_code,
                'download_count': subtitle.download_count,
                'rating': subtitle.rating,
                'format': subtitle.format,
                'encoding': subtitle.encoding,
                'file_size': subtitle.file_size,
                'fps': subtitle.fps,
                'release_info': subtitle.release_info,
                'uploader': subtitle.uploader
            })

        return jsonify({
            "success": True,
            "subtitles": subtitle_list,
            "count": len(subtitle_list)
        })

    except Exception as e:
        print(f"Error in subtitle search API: {e}")
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/subtitles/download', methods=['POST'])
def download_subtitle_api():
    """API endpoint for subtitle download."""
    try:
        data = request.get_json()
        if not data or not data.get('subtitle_id'):
            return jsonify({"success": False, "error": "Missing subtitle_id"})

        subtitle_id = data.get('subtitle_id')

        # Najdi titulky podle ID (zjednodušené - v reálné aplikaci byste si uložili informace o titulcích)
        # Pro nyní vrátíme placeholder odpověď
        return jsonify({
            "success": True,
            "download_url": f"/api/subtitles/file/{subtitle_id}",
            "message": "Subtitle prepared for download"
        })

    except Exception as e:
        print(f"Error in subtitle download API: {e}")
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/subtitles/languages')
def get_subtitle_languages():
    """Get supported subtitle languages."""
    try:
        languages = subtitle_manager.get_supported_languages()
        return jsonify({
            "success": True,
            "languages": languages,
            "default_languages": subtitle_manager.languages
        })
    except Exception as e:
        print(f"Error getting subtitle languages: {e}")
        return jsonify({"success": False, "error": str(e)})


@app.route('/static/subtitles/<filename>')
def serve_subtitle_file(filename):
    """Serve subtitle files."""
    try:
        subtitles_dir = os.path.join(app.root_path, 'static', 'subtitles')
        file_path = os.path.join(subtitles_dir, filename)

        if os.path.exists(file_path):
            return send_file(file_path)
        else:
            abort(404)
    except Exception as e:
        print(f"Error serving subtitle file: {e}")
        abort(500)


def _render_title_template(data: Dict[str, Any], media_type: str, tmdb_id: int, **kwargs) -> str:
    """Render title template with common data."""

    # Získáme detailní informace včetně credits
    detailed_data = tmdb_client.get_movie(tmdb_id) if media_type == 'movie' else tmdb_client.get_tv(tmdb_id)

    # Přidáme informace o profilech herců
    cast_with_images = []
    for actor in detailed_data.get('credits', {}).get('cast', [])[:12]:  # Zvýšíme na 12
        cast_with_images.append({
            'name': actor.get('name', ''),
            'character': actor.get('character', ''),
            'profile_path': tmdb_profile_image(actor.get('profile_path')),
            'id': actor.get('id')
        })

    # Přidáme informace o profilech štábu
    crew_with_images = []
    for member in detailed_data.get('credits', {}).get('crew', [])[:8]:  # Zvýšíme na 8
        crew_with_images.append({
            'name': member.get('name', ''),
            'job': member.get('job', ''),
            'department': member.get('department', ''),
            'profile_path': tmdb_profile_image(member.get('profile_path')),
            'id': member.get('id')
        })

    # Přidáme informace o titulcích
    subtitles_enabled = subtitle_manager.is_enabled()
    supported_languages = subtitle_manager.get_supported_languages() if subtitles_enabled else {}

    template_data = {
        'title': data.get('title') or data.get('name'),
        'english_title': data.get('original_title') or data.get('original_name'),
        'year': (data.get('release_date') or data.get('first_air_date', ''))[:4],
        'overview': data.get('overview'),
        'poster': tmdb_poster(data.get('poster_path')),
        'backdrop': tmdb_poster(data.get('backdrop_path'), size="w1280") if data.get('backdrop_path') else None,
        'tmdb_id': tmdb_id,
        'media_type': media_type,
        'seasons': data.get('seasons') if media_type == 'tv' else None,
        'imdb_url': imdb_url_from_tmdb_movie(tmdb_id) if media_type == 'movie' else None,
        'rating': data.get('vote_average'),
        'rating_formatted': format_rating(data.get('vote_average')),
        'genres': data.get('genres', []),
        'cast': cast_with_images,  # Nyní s fotkami
        'crew': crew_with_images,  # Nyní s fotkami

        # Informace o titulcích
        'subtitles_enabled': subtitles_enabled,
        'supported_languages': supported_languages,

        # Ostatní informace...
        'runtime': data.get('runtime'),
        'status': data.get('status'),
        'tagline': data.get('tagline'),
        'budget': data.get('budget'),
        'revenue': data.get('revenue'),
        'production_companies': data.get('production_companies', [])[:3],
        'production_countries': data.get('production_countries', [])[:3],
        'spoken_languages': data.get('spoken_languages', [])[:3],
        'vote_count': data.get('vote_count'),
        'popularity': data.get('popularity'),
        'adult': data.get('adult'),

        # Pro seriály:
        'episode_run_time': data.get('episode_run_time', []),
        'number_of_seasons': data.get('number_of_seasons'),
        'number_of_episodes': data.get('number_of_episodes'),
        'in_production': data.get('in_production'),
        'networks': data.get('networks', [])[:3],
        'created_by': data.get('created_by', [])[:3],
        'last_air_date': data.get('last_air_date'),
        'next_episode_to_air': data.get('next_episode_to_air'),
    }
    template_data.update(kwargs)

    return render_template('title.html', **template_data)


def _render_title_with_result(data: Dict[str, Any], media_type: str, tmdb_id: int, result: Dict[str, Any]) -> str:
    """Render title template with torrent operation result."""
    extra_data = {}

    if result["success"]:
        extra_data.update({
            'stream_url': result["stream_url"],
            'torrent_hash': result.get("hash"),
            'torrserver_url': TORRSERVER_URL,
            'warning': result.get("warning")
        })
    else:
        extra_data['error'] = result["error"]

    return _render_title_template(data, media_type, tmdb_id, **extra_data)


if __name__ == '__main__':
    # Vytvoř adresář pro titulky
    import os
    os.makedirs(os.path.join(app.root_path, 'static', 'subtitles'), exist_ok=True)

    app.run(debug=True)