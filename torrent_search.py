import requests
from typing import List, Dict, Any
import re
from urllib.parse import quote
from config import TORRENT_TIMEOUT, MAX_TORRENT_RESULTS
import time


class TorrentSearcher:
    """YTS pro filmy + TPB pro seriály"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        self.session.timeout = 8

        # TPB mirrors pro seriály
        self.tpb_mirrors = [
            "https://thepiratebay.org",
            "https://tpb.party",
            "https://thepiratebay10.org",
            "https://piratebay.live",
            "https://thepiratebay.zone"
        ]

    def search_torrents(self, title: str, year: str = "", media_type: str = "movie",
                        season: int = None, episode: int = None) -> List[Dict[str, Any]]:
        """Hlavní vyhledávání - YTS pro filmy, TPB pro seriály"""

        if media_type == "movie":
            print(f"🎬 YTS hledání filmů: {title} {year}")
            yts_results = self._search_yts_movies(title, year)

            if yts_results:
                print(f"✅ YTS: {len(yts_results)} filmů nalezeno")
                return yts_results
            else:
                print("⚠️ YTS nevrátil nic, zkouším TPB jako backup")
                return self._search_tpb(f"{title} {year}", media_type)
        else:
            print(f"📺 TPB hledání seriálů: {title}")
            return self._search_tv_shows(title, season, episode)

    def _search_yts_movies(self, title: str, year: str) -> List[Dict[str, Any]]:
        """YTS API pro filmy - priorita #1"""
        try:
            print("📡 YTS API...")
            params = {
                "query_term": f"{title} {year}" if year else title,
                "limit": 15,
                "sort_by": "seeds"
            }

            response = self.session.get("https://yts.mx/api/v2/list_movies.json", params=params, timeout=6)
            if response.status_code != 200:
                return []

            data = response.json()
            if not (data.get("status") == "ok" and data.get("data", {}).get("movies")):
                return []

            torrents = []
            for movie in data["data"]["movies"][:10]:
                for torrent in movie.get("torrents", []):
                    if torrent.get("hash"):
                        torrents.append({
                            "name": f"{movie['title']} ({movie['year']}) {torrent.get('quality', '')} [YTS]",
                            "magnet": self._build_magnet(torrent["hash"], movie['title']),
                            "size": torrent.get("size", "N/A"),
                            "seeders": torrent.get("seeds", 0),
                            "leechers": torrent.get("peers", 0),
                            "source": "YTS",
                            "hash": torrent["hash"],
                            "quality": torrent.get("quality", "Unknown")
                        })

            print(f"✅ YTS: {len(torrents)} filmových torrentů")
            return torrents

        except Exception as e:
            print(f"❌ YTS: {e}")
            return []

    def _search_tv_shows(self, title: str, season: int = None, episode: int = None) -> List[Dict[str, Any]]:
        """TPB pro seriály - používá ANGLICKÝ název"""

        # Debug - zkontrolujeme co dostáváme
        print(
            f"📺 TV search input: '{title}', S{season:02d}E{episode:02d}" if season and episode else f"📺 TV search input: '{title}'")

        # Sestavíme search query pro seriál
        if season and episode:
            search_query = f"{title} S{season:02d}E{episode:02d}"
            print(f"🔍 Hledám konkrétní epizodu: {search_query}")
        elif season:
            search_query = f"{title} Season {season}"
            print(f"🔍 Hledám celou sezónu: {search_query}")
        else:
            search_query = title
            print(f"🔍 Hledám seriál obecně: {search_query}")

        return self._search_tpb(search_query, "tv")

    def _search_tpb(self, query: str, media_type: str) -> List[Dict[str, Any]]:
        """The Pirate Bay search"""

        for mirror in self.tpb_mirrors:
            try:
                print(f"🌐 TPB: {mirror}")
                torrents = self._try_tpb_mirror(mirror, query, media_type)

                if torrents:
                    print(f"✅ {mirror}: {len(torrents)} torrentů")
                    return torrents
                else:
                    print(f"❌ {mirror}: nic nenalezeno")

                time.sleep(0.3)  # Krátká pauza

            except Exception as e:
                print(f"❌ {mirror}: {str(e)[:50]}")
                continue

        print("❌ Všechny TPB mirrors selhaly")
        return []

    def _try_tpb_mirror(self, base_url: str, query: str, media_type: str) -> List[Dict[str, Any]]:
        """Zkusí jeden TPB mirror"""

        # TPB kategorie
        category = "200" if media_type == "tv" else "201"  # TV / Movies
        search_url = f"{base_url}/search/{quote(query)}/1/99/{category}"

        print(f"📡 GET: {search_url}")

        response = self.session.get(search_url, timeout=6)
        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}")

        return self._parse_tpb_html(response.text, base_url)

    def _parse_tpb_html(self, html: str, base_url: str) -> List[Dict[str, Any]]:
        """Parsuje TPB HTML"""

        torrents = []

        try:
            # Najdeme searchResult tabulku
            table_match = re.search(r'<table[^>]*id="searchResult"[^>]*>(.*?)</table>', html, re.DOTALL)
            if not table_match:
                return []

            table_html = table_match.group(1)

            # Všechny řádky
            row_pattern = r'<tr[^>]*>(.*?)</tr>'
            rows = re.findall(row_pattern, table_html, re.DOTALL)

            for row in rows[:12]:  # Max 12 torrentů
                torrent = self._parse_tpb_row(row, base_url)
                if torrent:
                    torrents.append(torrent)

            return torrents

        except Exception as e:
            print(f"❌ TPB parsing: {e}")
            return []

    def _parse_tpb_row(self, row_html: str, base_url: str) -> Dict[str, Any]:
        """Parsuje jeden TPB řádek"""

        try:
            # Název
            name_match = re.search(r'<a[^>]*class="detLink"[^>]*title="[^"]*Details for ([^"]*)"[^>]*>', row_html)
            if not name_match:
                return None

            name = name_match.group(1).strip()

            # Magnet link
            magnet_match = re.search(r'<a[^>]*href="(magnet:\?xt=urn:btih:[^"]*)"[^>]*>', row_html)
            if not magnet_match:
                return None

            magnet = magnet_match.group(1)

            # Seeders
            seed_match = re.search(r'<td[^>]*align="right">(\d+)</td>', row_html)
            seeders = int(seed_match.group(1)) if seed_match else 0

            # Leechers (druhý align="right" td)
            leech_matches = re.findall(r'<td[^>]*align="right">(\d+)</td>', row_html)
            leechers = int(leech_matches[1]) if len(leech_matches) > 1 else 0

            # Velikost
            size_match = re.search(r'Size ([^,]+),', row_html)
            size = size_match.group(1).strip() if size_match else "Unknown"

            # Hash
            hash_match = re.search(r'urn:btih:([a-fA-F0-9]{40})', magnet)
            hash_str = hash_match.group(1) if hash_match else ""

            return {
                "name": f"{name} [TPB]",
                "magnet": magnet,
                "size": size,
                "seeders": seeders,
                "leechers": leechers,
                "source": "TPB",
                "hash": hash_str,
                "quality": self._extract_quality(name)
            }

        except Exception as e:
            print(f"❌ TPB row parsing: {e}")
            return None

    def _build_magnet(self, hash_str: str, name: str) -> str:
        """Sestaví magnet link pro YTS"""
        if not hash_str or len(hash_str) != 40:
            return ""

        # YTS trackery
        trackers = [
            "udp://tracker.openbittorrent.com:80",
            "udp://open.demonii.com:1337",
            "udp://tracker.coppersurfer.tk:6969",
            "udp://exodus.desync.com:6969"
        ]

        magnet = f"magnet:?xt=urn:btih:{hash_str}&dn={quote(name)}"
        for tracker in trackers:
            magnet += f"&tr={quote(tracker)}"

        return magnet

    def _extract_quality(self, name: str) -> str:
        """Extrahuje kvalitu z názvu"""
        name_lower = name.lower()

        if "2160p" in name_lower or "4k" in name_lower:
            return "4K"
        elif "1080p" in name_lower:
            return "1080p"
        elif "720p" in name_lower:
            return "720p"
        elif "480p" in name_lower:
            return "SD"
        else:
            return "Unknown"

    def get_tracker_status(self) -> Dict[str, bool]:
        """Status check"""
        return {
            "YTS": True,
            "TPB": True
        }