"""Models of the local whisper.cpp server: download one and switch to it live.

install-whisper.sh keeps the models in ~/whisper.cpp/models and starts the server
with ggml-current.bin, a link to the chosen model. Picking another quality in
Settings downloads it (once), moves the link and tells the running server to
/load it - no restart. When the app can't reach that folder (Docker), the
choice applies the next time install-whisper.sh runs on the host.
On Windows (no symlinks; a running server keeps its model file locked) the
choice is the name in models/current.txt and windows/control.py restarts
the server on it.
"""

import json
import logging
import os
import shutil
import subprocess
import threading
import time

import requests

from .. import config

logger = logging.getLogger(__name__)

MODELS = {  # quality -> (file name, MB)
    "fast": ("tiny-q5_1", 31),
    "balanced": ("base-q5_1", 57),
    "best": ("small-q5_1", 181),
}
DEFAULT = "fast"
URL = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{name}.bin"


class WhisperModels:
    def __init__(self, directory: str | None = None):
        self.dir = directory or os.path.join(
            os.getenv("WHISPER_DIR") or os.path.expanduser("~/whisper.cpp"), "models"
        )
        self.state = "ready"  # "downloading" / "error: ..."
        self._lock = threading.Lock()

    @property
    def local(self) -> bool:
        """Whether the server's model folder is on this machine (not inside Docker)."""
        return os.path.isdir(self.dir)

    def current(self) -> str:
        try:  # Windows: the chosen model's name
            with open(os.path.join(self.dir, "current.txt"), encoding="utf-8") as f:
                name = f.read().strip()
            for quality, (model, _) in MODELS.items():
                if model == name:
                    return quality
        except OSError:
            pass
        link = os.path.join(self.dir, "ggml-current.bin")
        try:
            target = os.path.basename(os.path.realpath(link))
        except OSError:
            return DEFAULT
        for quality, (name, _) in MODELS.items():
            if target == f"ggml-{name}.bin":
                return quality
        return DEFAULT

    def select(self, quality: str, server_url: str) -> None:
        """Download (in the background if needed) and load `quality` into the server."""
        if quality not in MODELS or not self.local:
            return
        with self._lock:
            if self.state == "downloading":
                return
            self.state = "downloading"
        threading.Thread(
            target=self._select, args=(quality, server_url), daemon=True
        ).start()

    def _select(self, quality: str, server_url: str) -> None:
        name, _ = MODELS[quality]
        path = os.path.join(self.dir, f"ggml-{name}.bin")
        try:
            if not os.path.exists(path):
                part = path + ".part"
                with requests.get(URL.format(name=name), stream=True, timeout=60) as r:
                    r.raise_for_status()
                    with open(part, "wb") as f:
                        f.writelines(r.iter_content(1 << 20))
                os.replace(part, path)
            if os.name == "nt":
                with open(
                    os.path.join(self.dir, "current.txt"), "w", encoding="utf-8"
                ) as f:
                    f.write(name)
            else:
                link = os.path.join(self.dir, "ggml-current.bin")
                if os.path.lexists(link):
                    os.remove(link)
                os.symlink(os.path.basename(path), link)
            root = server_url.rstrip("/").removesuffix("/v1")
            if not self._restart_service():
                # No service to restart (dev machine): load it into the running server.
                response = requests.post(
                    f"{root}/load", files={"model": (None, path)}, timeout=120
                )
                response.raise_for_status()
            self._wait_for(root)
            self.state = "ready"
            logger.info("Whisper switched to %s", name)
        except Exception as e:  # download / server errors are shown in Settings
            logger.warning("Whisper model %s failed: %s", name, e)
            self.state = f"error: {e}"

    @staticmethod
    def _restart_service() -> bool:
        """Termux runs the server as a runit service, Windows under windows/control.py
        (which starts it again on the new model within seconds): restart it."""
        if os.name == "nt":
            try:
                pid_file = os.path.join(config.BASE_DIR, "cache", "streamfinder.pid")
                with open(pid_file, encoding="utf-8") as f:
                    pid = json.load(f).get("whisper")
            except (OSError, ValueError):
                return False
            if not pid:
                return False
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return result.returncode == 0
        prefix = os.getenv("PREFIX", "")
        if "com.termux" not in prefix or not shutil.which("sv"):
            return False
        if not os.path.isdir(os.path.join(prefix, "var/service/streamfinder-whisper")):
            return False
        subprocess.run(
            ["sv", "restart", "streamfinder-whisper"], capture_output=True, timeout=30
        )
        return True

    @staticmethod
    def _wait_for(root: str, seconds: int = 60) -> None:
        for _ in range(seconds):
            try:
                requests.get(f"{root}/", timeout=2)
                return
            except requests.RequestException:
                time.sleep(1)
        raise RuntimeError("the Whisper server did not come back")
