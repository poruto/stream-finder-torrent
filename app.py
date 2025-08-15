from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory

from tmdb import (search_multi, get_movie, get_tv, get_tv_season, tmdb_poster,
                  imdb_url_from_tmdb_movie, discover_movies, discover_tv,
                  get_movie_genres, get_tv_genres, get_trending, get_popular_movies,
                  get_popular_tv, get_top_rated_movies, get_top_rated_tv,
                  get_now_playing_movies, get_upcoming_movies, format_rating)
from torrent_search import TorrentSearcher
from config import TORRSERVER_URL, TORRSERVER_STREAM_PATH, SUBTITLES_ENABLED
import re
import requests
import hashlib
import binascii

app = Flask(__name__)

# TorrentSearcher instance
torrent_searcher = TorrentSearcher()

# Regex pro magnet linky
MAGNET_RE = re.compile(r'magnet:\?xt=urn:btih:([a-fA-F0-9]{40}|[a-fA-F0-9]{32})')


def add_torrent_to_server(magnet_link):
    """
    Přidá torrent do TorrServer pomocí magnet linku a vrátí stream URL
    """
    try:
        # Validate magnet link
        if not magnet_link.startswith('magnet:'):
            return {"success": False, "error": "Neplatný magnet link"}

        match = MAGNET_RE.search(magnet_link)
        if not match:
            return {"success": False, "error": "Nelze extrahovat hash z magnet linku"}

        hash_string = match.group(1)

        # Konverze 32-hex na 40-hex pokud je potřeba
        if len(hash_string) == 32:
            try:
                # Base32 decode to bytes then hex encode to get 40-char hash
                import base64
                decoded = base64.b32decode(hash_string + '=' * (8 - len(hash_string) % 8))
                hash_string = decoded.hex()
            except:
                # Fallback - převod z base32 hex na standardní hex
                try:
                    hash_bytes = binascii.unhexlify(hash_string)
                    hash_string = binascii.hexlify(hash_bytes).decode('ascii')
                except:
                    pass

        print(f"🧲 Magnet hash: {hash_string}")

        # TorrServer API endpoints - OPRAVA!
        torrserver_base = TORRSERVER_URL.rstrip('/')
        add_url = f"{torrserver_base}/torrents"  # Opraveno: /torrents
        list_url = f"{torrserver_base}/torrents"  # Opraveno: /torrents

        # Nejprve zkontroluj, jestli torrent už existuje
        try:
            list_response = requests.get(list_url, timeout=10)
            if list_response.status_code == 200:
                existing_torrents = list_response.json()
                for torrent in existing_torrents:
                    if torrent.get('hash', '').lower() == hash_string.lower():
                        print(f"✅ Torrent již existuje v TorrServer")

                        # Zkus původní způsob stream URL (jak to fungovalo předtím)
                        torrent_name = torrent.get('name', f'torrent_{hash_string[:8]}')
                        stream_url = f"{torrserver_base}{TORRSERVER_STREAM_PATH}?link={hash_string}&index=1&play"

                        return {
                            "success": True,
                            "stream_url": stream_url,
                            "hash": hash_string,
                            "message": "Torrent již byl přidán, stream je připraven"
                        }
        except Exception as e:
            print(f"⚠️ Nelze zkontrolovat existující torrenty: {e}")

        # Přidat nový torrent
        add_data = {
            "action": "add",
            "link": magnet_link,
            "save_to_db": True
        }

        print(f"🔄 Přidávám torrent do TorrServer...")
        add_response = requests.post(add_url, json=add_data, timeout=30)

        print(f"📡 TorrServer odpověď: {add_response.status_code}")

        if add_response.status_code == 200:
            response_data = add_response.json()
            print(f"📦 Response data: {response_data}")

            # Původní způsob stream URL (jak to fungovalo)
            stream_url = f"{torrserver_base}{TORRSERVER_STREAM_PATH}?link={hash_string}&index=1&play"

            return {
                "success": True,
                "stream_url": stream_url,
                "hash": hash_string,
                "message": f"Torrent byl úspěšně přidán"
            }

        else:
            error_text = add_response.text
            print(f"❌ TorrServer error: {error_text}")
            return {
                "success": False,
                "error": f"TorrServer chyba ({add_response.status_code}): {error_text}"
            }

    except requests.exceptions.Timeout:
        return {"success": False, "error": "TorrServer timeout - zkus to znovu"}
    except requests.exceptions.ConnectionError:
        return {"success": False, "error": f"Nelze se připojit k TorrServer ({TORRSERVER_URL})"}
    except Exception as e:
        print(f"💥 Neočekávaná chyba: {e}")
        return {"success": False, "error": f"Neočekávaná chyba: {str(e)}"}


def _is_video_file(filename):
    """Kontrola, jestli je soubor video"""
    video_extensions = ['.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v']
    return any(filename.lower().endswith(ext) for ext in video_extensions)


@app.route('/')
def index():
    query = request.args.get('q', '').strip()
    category = request.args.get('category', 'search')
    genre = request.args.get('genre', type=int)
    min_rating = request.args.get('min_rating', type=float, default=0)
    max_rating = request.args.get('max_rating', type=float, default=10)
    year = request.args.get('year', type=int)
    sort_by = request.args.get('sort_by', 'popularity.desc')
    page = request.args.get('page', type=int, default=1)

    items = []
    total_pages = 0
    current_page = page

    try:
        if query and category == 'search':
            # Klasické vyhledávání
            results = search_multi(query, page=page)
            items = _process_search_results(results.get('results', []))
            total_pages = results.get('total_pages', 1)

        elif category == 'trending':
            media_type = request.args.get('media_type', 'all')
            time_window = request.args.get('time_window', 'week')
            results = get_trending(media_type, time_window, page)
            items = _process_search_results(results.get('results', []))
            total_pages = results.get('total_pages', 1)

        elif category == 'popular_movies':
            results = get_popular_movies(page)
            items = _process_search_results(results.get('results', []))
            total_pages = results.get('total_pages', 1)

        elif category == 'popular_tv':
            results = get_popular_tv(page)
            items = _process_search_results(results.get('results', []))
            total_pages = results.get('total_pages', 1)

        elif category == 'top_rated_movies':
            results = get_top_rated_movies(page)
            items = _process_search_results(results.get('results', []))
            total_pages = results.get('total_pages', 1)

        elif category == 'top_rated_tv':
            results = get_top_rated_tv(page)
            items = _process_search_results(results.get('results', []))
            total_pages = results.get('total_pages', 1)

        elif category == 'now_playing':
            results = get_now_playing_movies(page)
            items = _process_search_results(results.get('results', []))
            total_pages = results.get('total_pages', 1)

        elif category == 'upcoming':
            results = get_upcoming_movies(page)
            items = _process_search_results(results.get('results', []))
            total_pages = results.get('total_pages', 1)

        elif category == 'discover_movies':
            genre_ids = [genre] if genre else None
            results = discover_movies(genre_ids, min_rating, max_rating, sort_by, year, page)
            items = _process_search_results(results.get('results', []))
            total_pages = results.get('total_pages', 1)

        elif category == 'discover_tv':
            genre_ids = [genre] if genre else None
            results = discover_tv(genre_ids, min_rating, max_rating, sort_by, year, page)
            items = _process_search_results(results.get('results', []))
            total_pages = results.get('total_pages', 1)

    except Exception as e:
        print(f"Chyba při vyhledávání: {e}")

    # Získáme žánry pro filtry
    movie_genres = get_movie_genres()
    tv_genres = get_tv_genres()

    return render_template('index.html',
                           items=items,
                           q=query,
                           category=category,
                           genre=genre,
                           min_rating=min_rating,
                           max_rating=max_rating,
                           year=year,
                           sort_by=sort_by,
                           current_page=current_page,
                           total_pages=min(total_pages, 500),  # TMDB limit
                           movie_genres=movie_genres,
                           tv_genres=tv_genres)


def _process_search_results(results):
    """Zpracuje výsledky vyhledávání"""
    items = []
    for r in results:
        if r.get('media_type') in ['movie', 'tv'] or 'release_date' in r or 'first_air_date' in r:
            # Určíme media_type pokud není explicitně uvedeno
            if 'media_type' not in r:
                r['media_type'] = 'movie' if 'release_date' in r else 'tv'

            item = {
                'tmdb_id': r['id'],
                'media_type': r['media_type'],
                'title': r.get('title') or r.get('name', 'Bez názvu'),
                'year': (r.get('release_date') or r.get('first_air_date', ''))[:4],
                'poster': tmdb_poster(r.get('poster_path')),
                'rating': r.get('vote_average', 0),
                'rating_formatted': format_rating(r.get('vote_average')),
                'genres': r.get('genre_ids', [])
            }
            items.append(item)
    return items


@app.route('/title/<media_type>/<int:tmdb_id>', methods=['GET', 'POST'])
def title_detail(media_type, tmdb_id):
    try:
        # Handle magnet form submission
        if request.method == 'POST':
            magnet = request.form.get('magnet', '').strip()
            if magnet:
                result = add_torrent_to_server(magnet)
                if result["success"]:
                    # Reload s výsledky
                    if media_type == 'movie':
                        data = get_movie(tmdb_id)
                    else:
                        data = get_tv(tmdb_id)

                    return render_template('title.html',
                                           title=data.get('title') or data.get('name'),
                                           english_title=data.get('original_title') or data.get('original_name'),
                                           year=(data.get('release_date') or data.get('first_air_date', ''))[:4],
                                           overview=data.get('overview'),
                                           poster=tmdb_poster(data.get('poster_path')),
                                           tmdb_id=tmdb_id,
                                           media_type=media_type,
                                           seasons=data.get('seasons') if media_type == 'tv' else None,
                                           imdb_url=imdb_url_from_tmdb_movie(
                                               tmdb_id) if media_type == 'movie' else None,
                                           stream_url=result["stream_url"],
                                           torrent_hash=result.get("hash"),
                                           torrserver_url=TORRSERVER_URL,
                                           warning=result.get("warning"),
                                           rating=data.get('vote_average'),
                                           rating_formatted=format_rating(data.get('vote_average')),
                                           genres=data.get('genres', []),
                                           cast=data.get('credits', {}).get('cast', [])[:10],  # Top 10 herců
                                           crew=data.get('credits', {}).get('crew', [])[:5]  # Top 5 crew
                                           )
                else:
                    # Reload s chybou
                    if media_type == 'movie':
                        data = get_movie(tmdb_id)
                    else:
                        data = get_tv(tmdb_id)

                    return render_template('title.html',
                                           title=data.get('title') or data.get('name'),
                                           english_title=data.get('original_title') or data.get('original_name'),
                                           year=(data.get('release_date') or data.get('first_air_date', ''))[:4],
                                           overview=data.get('overview'),
                                           poster=tmdb_poster(data.get('poster_path')),
                                           tmdb_id=tmdb_id,
                                           media_type=media_type,
                                           seasons=data.get('seasons') if media_type == 'tv' else None,
                                           imdb_url=imdb_url_from_tmdb_movie(
                                               tmdb_id) if media_type == 'movie' else None,
                                           error=result["error"],
                                           rating=data.get('vote_average'),
                                           rating_formatted=format_rating(data.get('vote_average')),
                                           genres=data.get('genres', []),
                                           cast=data.get('credits', {}).get('cast', [])[:10],
                                           crew=data.get('credits', {}).get('crew', [])[:5]
                                           )

        # Normal GET request
        if media_type == 'movie':
            data = get_movie(tmdb_id)
        elif media_type == 'tv':
            data = get_tv(tmdb_id)
        else:
            return "Neplatný media_type", 400

        return render_template('title.html',
                               title=data.get('title') or data.get('name'),
                               english_title=data.get('original_title') or data.get('original_name'),
                               year=(data.get('release_date') or data.get('first_air_date', ''))[:4],
                               overview=data.get('overview'),
                               poster=tmdb_poster(data.get('poster_path')),
                               tmdb_id=tmdb_id,
                               media_type=media_type,
                               seasons=data.get('seasons') if media_type == 'tv' else None,
                               imdb_url=imdb_url_from_tmdb_movie(tmdb_id) if media_type == 'movie' else None,
                               rating=data.get('vote_average'),
                               rating_formatted=format_rating(data.get('vote_average')),
                               genres=data.get('genres', []),
                               cast=data.get('credits', {}).get('cast', [])[:10],  # Top 10 herců
                               crew=data.get('credits', {}).get('crew', [])[:5]  # Top 5 crew
                               )

    except Exception as e:
        print(f"Chyba v title_detail: {e}")
        return f"Chyba: {str(e)}", 500


@app.route('/season/<int:tmdb_id>/<int:season_number>')
def season_detail(tmdb_id, season_number):
    try:
        # Získej základní info o seriálu
        tv_data = get_tv(tmdb_id)
        title = tv_data.get('name', 'Neznámý seriál')
        english_title = tv_data.get('original_name', title)

        # Získej detaily sezóny
        season_data = get_tv_season(tmdb_id, season_number)

        return render_template('season.html',
                               title=title,
                               english_title=english_title,
                               season_name=season_data.get('name', f'Sezóna {season_number}'),
                               season_number=season_number,
                               episodes=season_data.get('episodes', []),
                               poster=tmdb_poster(season_data.get('poster_path')),
                               tmdb_id=tmdb_id,
                               torrserver_url=TORRSERVER_URL
                               )

    except Exception as e:
        print(f"Chyba v season_detail: {e}")
        return f"Chyba: {str(e)}", 500


@app.route('/episode-torrents')
def episode_torrents():
    """
    Stránka pro vyhledávání torrentů pro konkrétní epizodu
    """
    try:
        title = request.args.get('title', '')
        english_title = request.args.get('english_title', title)
        season = request.args.get('season', type=int)
        episode = request.args.get('episode', type=int)
        episode_name = request.args.get('episodeName', '')
        tmdb_id = request.args.get('tmdb_id', type=int)

        if not title or season is None or episode is None:
            return "Chybí parametry", 400

        # Použijeme sjednocený template s mode='episode_torrents'
        return render_template('title.html',
                               mode='episode_torrents',
                               title=title,
                               english_title=english_title,
                               season=season,
                               episode=episode,
                               episode_name=episode_name,
                               tmdb_id=tmdb_id,
                               media_type='tv',
                               TORRSERVER_URL=TORRSERVER_URL,
                               )

    except Exception as e:
        print(f"Chyba v episode_torrents: {e}")
        return f"Chyba: {str(e)}", 500


@app.route('/api/torrents', methods=['POST'])
def search_torrents_api():
    try:
        data = request.get_json()
        if not data:
            return jsonify([])

        title = data.get('title', '')
        english_title = data.get('english_title', title)
        year = data.get('year', '')
        media_type = data.get('media_type', 'movie')
        season = data.get('season')
        episode = data.get('episode')

        if not title:
            return jsonify([])

        print(f"🔍 Vyhledávám torrenty pro: {title} ({english_title})")

        # Volej vyhledávač
        torrents = torrent_searcher.search_torrents(
            title=english_title or title,  # Přednostně anglický název
            year=year,
            media_type=media_type,
            season=season,
            episode=episode
        )

        print(f"📦 Nalezeno {len(torrents)} torrentů")
        return jsonify(torrents)

    except Exception as e:
        print(f"❌ Chyba při vyhledávání torrentů: {e}")
        return jsonify([])


@app.route('/api/play-torrent', methods=['POST'])
def play_torrent_api():
    """
    API pro přidání torrenta do TorrServer (AJAX)
    """
    try:
        data = request.get_json()
        magnet = data.get('magnet', '').strip()

        if not magnet:
            return jsonify({"success": False, "error": "Chybí magnet link"})

        result = add_torrent_to_server(magnet)
        return jsonify(result)

    except Exception as e:
        print(f"❌ API play-torrent chyba: {e}")
        return jsonify({"success": False, "error": str(e)})


@app.route('/api/torrserver-status')
def torrserver_status():
    try:
        response = requests.get(f"{TORRSERVER_URL.rstrip('/')}/echo", timeout=5)
        if response.status_code == 200:
            return jsonify({"status": "online", "url": TORRSERVER_URL})
        else:
            return jsonify({"status": "offline", "url": TORRSERVER_URL})
    except:
        return jsonify({"status": "offline", "url": TORRSERVER_URL})


@app.route('/api/tracker-status')
def tracker_status():
    try:
        status = torrent_searcher.get_tracker_status()
        return jsonify(status)
    except Exception as e:
        return jsonify({"error": str(e)})


if __name__ == '__main__':
    app.run(debug=True)