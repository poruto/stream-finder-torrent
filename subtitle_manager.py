"""Subtitle manager for downloading and managing subtitles."""

import os
import re
import hashlib
from typing import Dict, List, Optional, Any, Union
import requests
from dataclasses import dataclass
from config import config

@dataclass
class SubtitleInfo:
    """Subtitle information data class."""
    id: str
    name: str
    language: str
    language_code: str
    download_count: int
    rating: float
    format: str
    encoding: str
    download_link: str
    file_size: int = 0
    fps: Optional[float] = None
    release_info: str = ""
    uploader: str = ""

class SubtitleManager:
    """Manages subtitle operations including search and download."""
    
    def __init__(self):
        self.api_key = config.OPENSUBTITLES_API_KEY
        self.enabled = config.SUBTITLES_ENABLED
        self.languages = config.SUBTITLE_LANGUAGES
        self.base_url = "https://api.opensubtitles.com/api/v1"
        self.session = requests.Session()
        
        # Nastavení hlaviček pro OpenSubtitles API
        if self.api_key:
            self.session.headers.update({
                'Api-Key': self.api_key,
                'Content-Type': 'application/json',
                'User-Agent': 'TorrentStreamApp v1.0'
            })
    
    def is_enabled(self) -> bool:
        """Check if subtitles are enabled."""
        return self.enabled and bool(self.api_key)
    
    def search_subtitles(self, 
                        imdb_id: Optional[str] = None,
                        tmdb_id: Optional[int] = None,
                        query: Optional[str] = None,
                        year: Optional[int] = None,
                        season_number: Optional[int] = None,
                        episode_number: Optional[int] = None,
                        languages: Optional[List[str]] = None) -> List[SubtitleInfo]:
        """
        Search for subtitles using various parameters.
        
        Args:
            imdb_id: IMDB ID (without 'tt' prefix)
            tmdb_id: TMDB ID
            query: Search query (movie/show title)
            year: Release year
            season_number: Season number (for TV shows)
            episode_number: Episode number (for TV shows)
            languages: List of language codes (e.g., ['en', 'cs'])
        
        Returns:
            List of SubtitleInfo objects
        """
        if not self.is_enabled():
            return []
        
        # Použij defaultní jazyky pokud nejsou specifikovány
        if not languages:
            languages = self.languages
        
        params = {
            'languages': ','.join(languages),
            'order_by': 'download_count'
        }
        
        # Přidej parametry podle dostupných informací
        if imdb_id:
            # Odeber 'tt' prefix pokud existuje
            clean_imdb = imdb_id.replace('tt', '')
            params['imdb_id'] = clean_imdb
        elif tmdb_id:
            params['tmdb_id'] = tmdb_id
        elif query:
            params['query'] = query
            if year:
                params['year'] = year
        
        # Pro epizody seriálů
        if season_number is not None:
            params['season_number'] = season_number
        if episode_number is not None:
            params['episode_number'] = episode_number
        
        try:
            response = self.session.get(f"{self.base_url}/subtitles", params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            subtitles = []
            
            for item in data.get('data', []):
                subtitle = self._parse_subtitle_data(item)
                if subtitle:
                    subtitles.append(subtitle)
            
            return subtitles
            
        except requests.RequestException as e:
            print(f"Chyba při vyhledávání titulků: {e}")
            return []
    
    def _parse_subtitle_data(self, data: Dict[str, Any]) -> Optional[SubtitleInfo]:
        """Parse subtitle data from API response."""
        try:
            attributes = data.get('attributes', {})
            
            return SubtitleInfo(
                id=data.get('id', ''),
                name=attributes.get('release', 'Neznámý'),
                language=attributes.get('language', 'Neznámý'),
                language_code=attributes.get('language', 'un'),
                download_count=attributes.get('download_count', 0),
                rating=attributes.get('ratings', 0.0),
                format=attributes.get('format', 'srt'),
                encoding=attributes.get('encoding', 'utf-8'),
                download_link=attributes.get('url', ''),
                file_size=attributes.get('file_size', 0),
                fps=attributes.get('fps'),
                release_info=attributes.get('release', ''),
                uploader=attributes.get('uploader', {}).get('name', 'Neznámý')
            )
        except Exception as e:
            print(f"Chyba při parsování titulků: {e}")
            return None
    
    def download_subtitle(self, subtitle: SubtitleInfo, output_dir: str = "subtitles") -> Optional[str]:
        """
        Download subtitle file.
        
        Args:
            subtitle: SubtitleInfo object
            output_dir: Directory to save subtitle file
        
        Returns:
            Path to downloaded file or None if failed
        """
        if not self.is_enabled() or not subtitle.download_link:
            return None
        
        try:
            # Vytvoř adresář pokud neexistuje
            os.makedirs(output_dir, exist_ok=True)
            
            # Vygeneruj název souboru
            safe_name = self._sanitize_filename(subtitle.name)
            filename = f"{safe_name}.{subtitle.language_code}.{subtitle.format}"
            filepath = os.path.join(output_dir, filename)
            
            # Stáhni soubor
            response = self.session.get(subtitle.download_link, timeout=30)
            response.raise_for_status()
            
            # Ulož soubor
            with open(filepath, 'wb') as f:
                f.write(response.content)
            
            print(f"Titulky staženy: {filepath}")
            return filepath
            
        except Exception as e:
            print(f"Chyba při stahování titulků: {e}")
            return None
    
    def get_subtitle_url_for_video(self, subtitle_path: str) -> str:
        """
        Get URL for subtitle file that can be used in video player.
        
        Args:
            subtitle_path: Path to subtitle file
        
        Returns:
            URL for subtitle file
        """
        # Vytvoř relativní cestu k titulkům
        if subtitle_path.startswith('subtitles/'):
            return f"/static/{subtitle_path}"
        else:
            return f"/static/subtitles/{os.path.basename(subtitle_path)}"
    
    def _sanitize_filename(self, filename: str) -> str:
        """Sanitize filename for safe file system usage."""
        # Odeber nebezpečné znaky
        safe_name = re.sub(r'[<>:"/\\|?*]', '', filename)
        safe_name = re.sub(r'\s+', '_', safe_name.strip())
        return safe_name[:100]  # Omez délku názvu
    
    def search_by_hash(self, file_hash: str, file_size: int) -> List[SubtitleInfo]:
        """
        Search subtitles by file hash (most accurate method).
        
        Args:
            file_hash: MD5 hash of the video file
            file_size: Size of video file in bytes
        
        Returns:
            List of SubtitleInfo objects
        """
        if not self.is_enabled():
            return []
        
        params = {
            'moviehash': file_hash,
            'moviebytesize': file_size,
            'languages': ','.join(self.languages)
        }
        
        try:
            response = self.session.get(f"{self.base_url}/subtitles", params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            subtitles = []
            
            for item in data.get('data', []):
                subtitle = self._parse_subtitle_data(item)
                if subtitle:
                    subtitles.append(subtitle)
            
            return subtitles
            
        except requests.RequestException as e:
            print(f"Chyba při vyhledávání titulků podle hash: {e}")
            return []
    
    def calculate_video_hash(self, video_url: str, file_size: int) -> Optional[str]:
        """
        Calculate OpenSubtitles hash for video file (simplified version).
        Note: This is a simplified implementation. For accurate hashing,
        you would need access to the actual video file.
        """
        try:
            # Pro stream URL nemůžeme spočítat přesný hash
            # Používáme náhradní hash basovaný na URL
            hash_input = f"{video_url}_{file_size}".encode('utf-8')
            return hashlib.md5(hash_input).hexdigest()[:16]
        except Exception:
            return None
    
    def get_supported_languages(self) -> Dict[str, str]:
        """Get list of supported languages."""
        return {
            'cs': 'Čeština',
            'en': 'English', 
            'sk': 'Slovenčina',
            'de': 'Deutsch',
            'fr': 'Français',
            'es': 'Español',
            'it': 'Italiano',
            'ru': 'Русский',
            'pl': 'Polski',
            'hu': 'Magyar'
        }
