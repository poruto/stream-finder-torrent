"""OpenSubtitles.com REST API v1 client (https://opensubtitles.stoplight.io)."""

import threading
import time
from typing import Any

import requests


class OpenSubtitlesClient:
    """OpenSubtitles.com REST API v1 (https://opensubtitles.stoplight.io)."""

    BASE_URL = "https://api.opensubtitles.com/api/v1"

    def __init__(
        self,
        api_key: str,
        username: str = "",
        password: str = "",
        user_agent: str = "StreamFinder v1.0",
    ):
        self.api_key = api_key
        self.username = username
        self.password = password
        self.base_url = self.BASE_URL
        self.token: str | None = None
        self._token_expires = 0.0
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers.update(
            {"Api-Key": api_key, "User-Agent": user_agent, "Accept": "application/json"}
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _auth_headers(self) -> dict[str, str]:
        """Log in (if credentials are configured) for a higher download quota."""
        if not (self.username and self.password):
            return {}
        with self._lock:
            if not self.token or time.time() > self._token_expires:
                response = self.session.post(
                    f"{self.BASE_URL}/login",
                    timeout=15,
                    json={"username": self.username, "password": self.password},
                )
                response.raise_for_status()
                data = response.json()
                self.token = data["token"]
                if data.get("base_url"):
                    self.base_url = (
                        f"https://{data['base_url'].removeprefix('https://')}/api/v1"
                    )
                self._token_expires = time.time() + 12 * 3600
        return {"Authorization": f"Bearer {self.token}"}

    def search(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        # The API 301-redirects unless params are lowercase and alphabetically sorted.
        clean = sorted(
            (k, str(v).lower()) for k, v in params.items() if v not in (None, "")
        )
        response = self.session.get(
            f"{self.base_url}/subtitles", params=clean, timeout=15
        )
        response.raise_for_status()
        return response.json().get("data", [])

    def download(self, file_id: int) -> bytes:
        headers = self._auth_headers()
        response = self.session.post(
            f"{self.base_url}/download",
            json={"file_id": file_id},
            headers=headers,
            timeout=15,
        )
        if response.status_code == 406:
            raise RuntimeError("Vyčerpán denní limit stahování z OpenSubtitles")
        response.raise_for_status()
        link = response.json().get("link")
        if not link:
            raise RuntimeError(
                response.json().get("message", "OpenSubtitles nevrátil odkaz")
            )
        file_response = requests.get(link, timeout=30)
        file_response.raise_for_status()
        return file_response.content
