"""StreamFinder on Windows: TorrServer, the app and local Whisper in the background.

    streamfinder start | stop | restart | status | update | logs

Two ways to get it, the same commands:
- the portable app from GitHub Releases (windows/build-release.ps1): unzip, run
  StreamFinder.exe. Python, TorrServer and Whisper are inside, no git; it updates
  by unpacking the newest release over its folder.
- windows-install.ps1: a git clone with its own .venv, updated from GitHub master.
`tray` (StreamFinder.exe) starts `run`, opens the browser and stays in the tray.
`run` is the supervisor (pythonw, no window): it restarts whatever crashes and
checks for a new version every 30 minutes. `install` downloads what is missing
(TorrServer, Whisper) and asks for missing keys; updates run it again.
Logs: cache/logs/*.log
"""

import ctypes
import json
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import requests
from dotenv import dotenv_values

ENV_FILE = os.path.join(REPO, ".env")
CACHE = os.path.join(REPO, "cache")
LOGS = os.path.join(CACHE, "logs")
# {"supervisor": pid, "<service>": pid}; the app reads the Whisper pid to restart it
PID_FILE = os.path.join(CACHE, "streamfinder.pid")
TRAY_PID_FILE = os.path.join(CACHE, "tray.pid")
ICON = os.path.join(REPO, "windows", "streamfinder.ico")
STARTUP_LINK = os.path.join(
    os.getenv("APPDATA", ""),
    "Microsoft",
    "Windows",
    "Start Menu",
    "Programs",
    "Startup",
    "StreamFinder.lnk",
)
FAILED_FILE = os.path.join(CACHE, ".update-failed")
# A release has no .git: it updates from GitHub Releases instead of master.
RELEASE = not os.path.isdir(os.path.join(REPO, ".git"))
ROOT = os.path.dirname(REPO)  # the portable app's folder
LAUNCHER = os.path.join(ROOT, "StreamFinder.exe")
RELEASES_API = (
    "https://api.github.com/repos/poruto/stream-finder-torrent/releases/latest"
)
# The clone's virtual environment, or the Python bundled with a release.
SCRIPTS = os.path.join(REPO, ".venv", "Scripts")
if not os.path.isdir(SCRIPTS):
    SCRIPTS = os.path.dirname(sys.executable)
PYTHON = os.path.join(SCRIPTS, "python.exe")
PYTHONW = os.path.join(SCRIPTS, "pythonw.exe")
TORRSERVER = os.path.join(REPO, "bin", "torrserver.exe")
TORRSERVER_TAG = os.getenv("TORRSERVER_TAG", "MatriX.147")
WHISPER_DIR = os.getenv("WHISPER_DIR") or (
    os.path.join(os.path.dirname(REPO), "whisper")  # bundled next to the app
    if RELEASE
    else os.path.expanduser("~/whisper.cpp")
)
WHISPER_SERVER = os.path.join(WHISPER_DIR, "build", "bin", "whisper-server.exe")
WHISPER_MODELS = os.path.join(WHISPER_DIR, "models")
WHISPER_PORT = 8178
UPDATE_EVERY = 30 * 60

NO_WINDOW = subprocess.CREATE_NO_WINDOW
DETACHED = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

log = logging.getLogger("streamfinder.windows")


def _env() -> dict:
    return dotenv_values(ENV_FILE) if os.path.exists(ENV_FILE) else {}


def _app_port() -> int:
    try:
        return int(_env().get("PORT") or 5000)
    except ValueError:
        return 5000


def _git(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", REPO, *args],
        capture_output=True,
        text=True,
        creationflags=NO_WINDOW,
        **kwargs,
    )


# --- processes ---------------------------------------------------------------


def _image(pid: int) -> str | None:
    """Path of the running process `pid`, None when it is not running."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(
        0x1000, False, pid
    )  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        buffer = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_ulong(len(buffer))
        if code.value != 259 or not kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):  # 259 = STILL_ACTIVE
            return None
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def _pids() -> dict:
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _supervisor() -> int | None:
    """Pid of the running supervisor (a pid left over from before a reboot may be reused)."""
    pid = _pids().get("supervisor")
    image = _image(pid) if pid else None
    return pid if image and os.path.basename(image).lower() == "pythonw.exe" else None


def _kill_tree(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        creationflags=NO_WINDOW,
    )


def _kill_leftovers() -> None:
    """Services of a supervisor that did not stop them (killed, crashed)."""
    ours = tuple(os.path.normcase(d) for d in (REPO, WHISPER_DIR, SCRIPTS))
    for name, pid in _pids().items():
        image = _image(pid) if name != "supervisor" else None
        if image and os.path.normcase(image).startswith(ours):
            _kill_tree(pid)


def _detach(command: list[str]) -> None:
    try:  # out of the terminal's (or old supervisor's) job, so it outlives it
        subprocess.Popen(
            command,
            cwd=REPO,
            creationflags=DETACHED | subprocess.CREATE_BREAKAWAY_FROM_JOB,
        )
    except OSError:
        subprocess.Popen(command, cwd=REPO, creationflags=DETACHED)


def _launch_supervisor() -> None:
    _detach([PYTHONW, os.path.abspath(__file__), "run"])


# --- the supervisor ----------------------------------------------------------


def _whisper_command() -> list[str]:
    name = _whisper_model()
    # Physical cores: hyper-threads only slow Whisper down.
    threads = os.getenv("WHISPER_THREADS") or str(
        max(1, min(8, (os.cpu_count() or 4) // 2))
    )
    # Same options as whisper-run.sh: exact word timing (DTW) needs the model's own
    # preset and flash attention off; -nf: no temperature fallback in music scenes.
    return [
        WHISPER_SERVER,
        "-m",
        os.path.join(WHISPER_MODELS, f"ggml-{name}.bin"),
        "--host",
        "127.0.0.1",
        "--port",
        str(WHISPER_PORT),
        "-l",
        "auto",
        "-t",
        threads,
        "--inference-path",
        "/v1/audio/transcriptions",
        "-nf",
        "--dtw",
        name.split("-")[0],
        "-nfa",
    ]


def _services() -> dict[str, list[str]]:
    env = _env()
    listen = f"{env.get('HOST') or '0.0.0.0'}:{_app_port()}"
    data = os.path.join(REPO, "data", "torrserver")
    services = {
        "torrserver": [TORRSERVER, "-p", "8090", "-d", data, "-k"],
        # One process on purpose: subtitle metadata and caches live in its memory.
        "app": [
            PYTHON,
            "-m",
            "waitress",
            f"--listen={listen}",
            "--threads=16",
            "--channel-timeout=300",
            "app:app",
        ],
    }
    if _whisper_ready() and not os.path.exists(os.path.join(CACHE, ".no-whisper")):
        services["whisper"] = _whisper_command()
    return services


def _spawn(name: str, command: list[str]) -> subprocess.Popen:
    path = os.path.join(LOGS, f"{name}.log")
    if os.path.exists(path) and os.path.getsize(path) > 5 << 20:
        os.replace(path, path + ".1")
    with open(path, "ab") as output:
        return subprocess.Popen(
            command,
            cwd=REPO,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=dict(os.environ, PYTHONUNBUFFERED="1", WHISPER_DIR=WHISPER_DIR),
            creationflags=NO_WINDOW,
        )


def _write_pids(procs: dict[str, subprocess.Popen]) -> None:
    pids = {"supervisor": os.getpid(), **{n: p.pid for n, p in procs.items()}}
    with open(PID_FILE + ".tmp", "w", encoding="utf-8") as f:
        json.dump(pids, f)
    os.replace(PID_FILE + ".tmp", PID_FILE)


def _failed_before(target: str) -> bool:
    """A version that already failed is retried only after a newer one comes out."""
    try:
        with open(FAILED_FILE, encoding="utf-8") as f:
            return f.read().strip() == target
    except OSError:
        return False


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split(".") if part.isdigit())


def _new_release() -> str | None:
    """Download URL of the newest release's portable zip, when it is newer than this one."""
    from streamfinder.version import VERSION

    try:
        response = requests.get(RELEASES_API, timeout=30)
        response.raise_for_status()
        release = response.json()
    except (requests.RequestException, ValueError):
        return None
    if _version_key(release["tag_name"].lstrip("v")) <= _version_key(VERSION):
        return None
    url = next(
        (
            a["browser_download_url"]
            for a in release.get("assets", [])
            if a["name"].lower().endswith("-windows.zip")
        ),
        None,
    )
    return None if not url or _failed_before(url) else url


def _new_version() -> str | None:
    """What to update to: the newer release (zip URL) or master commit (clone)."""
    if RELEASE:
        return _new_release()
    if _git("fetch", "-q", "origin", "master", timeout=120).returncode:
        return None
    head = _git("rev-parse", "HEAD").stdout.strip()
    remote = _git("rev-parse", "FETCH_HEAD").stdout.strip()
    if not remote or remote == head or _failed_before(remote):
        return None
    # Never throw away local changes (a development clone).
    if _git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        return None
    return remote


def _ps(path: str) -> str:
    return "'" + path.replace("'", "''") + "'"


def _install_release(url: str, interactive: bool) -> bool:
    """Download the new portable zip; a helper unpacks it over this folder once every
    StreamFinder process has stopped (running files can't be replaced) and starts it."""
    archive = os.path.join(CACHE, "StreamFinder-update.zip")
    try:
        _download(url, archive)
    except requests.RequestException as e:
        log.error("downloading %s failed: %s", url, e)
        print(f"!! Downloading the new version failed: {e}")
        return False
    script = os.path.join(CACHE, "update.ps1")
    with open(script, "w", encoding="utf-8-sig") as f:
        f.write(
            f"""$ErrorActionPreference = 'Stop'
Start-Sleep -Seconds 3
# Everything that runs from this folder (tray, supervisor, app, TorrServer, Whisper).
$root = {_ps(ROOT + os.sep)}
for ($i = 0; $i -lt 20; $i++) {{
  $running = Get-Process | Where-Object {{ $_.Path -and $_.Path.StartsWith($root, 'OrdinalIgnoreCase') }}
  if (-not $running) {{ break }}
  $running | Stop-Process -Force -ErrorAction SilentlyContinue
  Start-Sleep -Seconds 1
}}
$unpacked = Join-Path $env:TEMP 'StreamFinder-update'
if (Test-Path $unpacked) {{ Remove-Item -Recurse -Force $unpacked }}
Expand-Archive -Force {_ps(archive)} $unpacked
$new = (Get-ChildItem $unpacked -Directory | Select-Object -First 1).FullName
# Files a new version no longer has must not linger; settings and cache stay.
Remove-Item -Recurse -Force {_ps(os.path.join(REPO, "streamfinder"))}, {_ps(os.path.join(ROOT, "python"))} -ErrorAction SilentlyContinue
Copy-Item -Recurse -Force (Join-Path $new '*') {_ps(ROOT)}
Remove-Item -Recurse -Force $unpacked, {_ps(archive)}
Start-Process {_ps(LAUNCHER)} -ArgumentList '--background'
"""
        )
    _detach(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            script,
        ]
    )
    return True


def _apply_update(target: str, interactive: bool) -> bool:
    """Check out commit `target`, install its dependencies and run its `install` step
    (a release: unpack the zip at URL `target`)."""
    if RELEASE:
        return _install_release(target, interactive)
    steps = [
        ["git", "-C", REPO, "reset", "-q", "--hard", target],
        [
            PYTHON,
            "-m",
            "pip",
            "install",
            "-q",
            "--disable-pip-version-check",
            "-r",
            os.path.join(REPO, "requirements.txt"),
        ],
        # The new version's own install step (new services, downloads, keys).
        [PYTHON, os.path.abspath(__file__), "install"],
    ]
    for step in steps:
        result = subprocess.run(
            step,
            cwd=REPO,
            stdin=None if interactive else subprocess.DEVNULL,
            capture_output=not interactive,
            text=True,
            creationflags=0 if interactive else NO_WINDOW,
        )
        if result.returncode:
            log.error(
                "update step %s failed:\n%s%s", step[1:3], result.stdout, result.stderr
            )
            with open(FAILED_FILE, "w", encoding="utf-8") as f:
                f.write(target)
            return False
    if os.path.exists(FAILED_FILE):
        os.remove(FAILED_FILE)
    return True


def run() -> None:
    """The supervisor: keeps every service running and installs new versions."""
    os.makedirs(LOGS, exist_ok=True)
    logging.basicConfig(
        filename=os.path.join(LOGS, "supervisor.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if _supervisor():
        return  # already running (logon shortcut and `streamfinder start`)
    _kill_leftovers()
    procs: dict[str, subprocess.Popen] = {}
    started: dict[str, float] = {}
    next_update = time.time() + 60
    log.info("supervisor started (pid %s)", os.getpid())
    try:
        while True:
            for name, command in _services().items():
                proc = procs.get(name)
                if proc and proc.poll() is None:
                    continue
                if time.time() - started.get(name, 0) < 10:
                    continue  # crashing right away: try again at most every 10 s
                if proc:
                    log.warning("%s exited with %s, restarting", name, proc.returncode)
                started[name] = time.time()
                try:
                    procs[name] = _spawn(name, command)
                except OSError as e:  # missing or blocked .exe (antivirus): retry later
                    log.error("%s could not start: %s", name, e)
                    procs.pop(name, None)
                    continue
                _write_pids(procs)
            if time.time() >= next_update:
                next_update = time.time() + UPDATE_EVERY
                target = _new_version()
                if target:
                    log.info("updating to %s", target)
                    for proc in procs.values():  # running files can't be replaced
                        _kill_tree(proc.pid)
                    _apply_update(target, interactive=False)
                    os.remove(PID_FILE)
                    if not RELEASE:  # the update helper starts the new version itself
                        _launch_supervisor()  # the new version's supervisor
                    return
            time.sleep(2)
    except Exception:
        log.exception("supervisor failed")
        for proc in procs.values():
            _kill_tree(proc.pid)
        raise


# --- install -----------------------------------------------------------------


def _download(url: str, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with requests.get(url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with open(path + ".part", "wb") as f:
            f.writelines(response.iter_content(1 << 20))
    os.replace(path + ".part", path)


def _install_torrserver() -> None:
    version_file = TORRSERVER + ".version"
    try:
        with open(version_file, encoding="utf-8") as f:
            if f.read().strip() == TORRSERVER_TAG and os.path.exists(TORRSERVER):
                return
    except OSError:
        pass
    # No Windows-on-ARM build: those run the x64 one.
    arch = "386" if platform.machine().lower() in ("x86", "i386", "i686") else "amd64"
    print(f"==> Downloading TorrServer {TORRSERVER_TAG}")
    _download(
        "https://github.com/YouROK/TorrServer/releases/download/"
        f"{TORRSERVER_TAG}/TorrServer-windows-{arch}.exe",
        TORRSERVER,
    )
    with open(version_file, "w", encoding="utf-8") as f:
        f.write(TORRSERVER_TAG)


def _create_env() -> None:
    if os.path.exists(ENV_FILE):
        return
    with open(os.path.join(REPO, ".env.example"), encoding="utf-8") as f:
        lines = f.read().splitlines()
    for i, line in enumerate(lines):
        if line.startswith("SECRET_KEY="):
            lines[i] = "SECRET_KEY=" + os.urandom(32).hex()
        elif line.startswith("HOST="):
            lines[i] = "HOST=0.0.0.0"  # phones and TVs on the network too
    with open(ENV_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def _whisper_model() -> str | None:
    """Name of the chosen model (models/current.txt, set by Settings) when downloaded."""
    from streamfinder.subtitles.whisper_models import MODELS, WhisperModels

    name = MODELS[WhisperModels(WHISPER_MODELS).current()][0]
    path = os.path.join(WHISPER_MODELS, f"ggml-{name}.bin")
    return name if os.path.exists(path) else None


def _whisper_ready() -> bool:
    return os.path.exists(WHISPER_SERVER) and _whisper_model() is not None


def _install_whisper() -> None:
    """Local Whisper for AI subtitles: whisper.cpp's Windows build and the fastest model."""
    from streamfinder import config
    from streamfinder.subtitles.whisper_models import DEFAULT, MODELS, URL

    if os.path.exists(os.path.join(CACHE, ".no-whisper")):
        return
    if not os.path.exists(WHISPER_SERVER):
        print("==> Downloading local Whisper for AI subtitles")
        asset = (
            "whisper-bin-win-cpu-arm64.zip"
            if platform.machine().lower() == "arm64"
            else "whisper-bin-x64.zip"
        )
        releases = requests.get(
            "https://api.github.com/repos/ggml-org/whisper.cpp/releases?per_page=20",
            timeout=30,
        )
        releases.raise_for_status()
        url = next(  # not every release has binaries
            a["browser_download_url"]
            for release in releases.json()
            for a in release.get("assets", [])
            if a["name"] == asset
        )
        archive = os.path.join(WHISPER_DIR, asset)
        _download(url, archive)
        bin_dir = os.path.dirname(WHISPER_SERVER)
        os.makedirs(bin_dir, exist_ok=True)
        with zipfile.ZipFile(archive) as z:
            for member in z.namelist():
                name = os.path.basename(member)
                if name == "whisper-server.exe" or name.endswith(".dll"):
                    with (
                        z.open(member) as src,
                        open(os.path.join(bin_dir, name), "wb") as dst,
                    ):
                        shutil.copyfileobj(src, dst)
        os.remove(archive)
    if not _whisper_model():
        name = MODELS[DEFAULT][0]
        model = os.path.join(WHISPER_MODELS, f"ggml-{name}.bin")
        if not os.path.exists(model):
            print(f"==> Downloading the {name} model")
            _download(URL.format(name=name), model)
        # The chosen model (Settings changes it); no symlinks needed.
        with open(
            os.path.join(WHISPER_MODELS, "current.txt"), "w", encoding="utf-8"
        ) as f:
            f.write(name)
    if not _env().get("TRANSCRIBE_PROVIDER"):
        config.save_settings(
            {
                "TRANSCRIBE_PROVIDER": "local",
                "TRANSCRIBE_API_URL": f"http://127.0.0.1:{WHISPER_PORT}/v1",
            }
        )


def ask_keys() -> bool:
    """Ask for the API keys and logins that are not set yet (as setup-keys.sh does).

    Whatever is skipped once (Enter) is not asked again. True = something was saved.
    """
    import getpass

    from streamfinder import config

    if not (sys.stdin and sys.stdin.isatty()):
        return False
    asked_file = os.path.join(CACHE, ".setup-asked")
    try:
        with open(asked_file, encoding="utf-8") as f:
            asked = set(f.read().split())
    except OSError:
        asked = set()
    env = _env()
    changes: dict[str, str] = {}
    intro = []

    def needs(group: str, *keys: str) -> bool:
        if group in asked or all(env.get(k) for k in keys):
            return False
        if not intro:
            intro.append(True)
            print(
                "==> Settings that are not filled in yet "
                "(Enter = skip, can be set later on the web)"
            )
        asked.add(group)
        return True

    def ask(prompt: str, secret: bool = False) -> str:
        try:
            return (
                (getpass.getpass(prompt) if secret else input(prompt))
                .strip()
                .replace("'", "")
            )
        except EOFError:
            return ""

    if needs("tmdb", "TMDB_API_KEY"):
        value = ask(
            "TMDB API key (required for the catalogue, "
            "https://www.themoviedb.org/settings/api): "
        )
        if value:
            changes["TMDB_API_KEY"] = value
    if needs("opensubtitles", "OPENSUBTITLES_API_KEY"):
        value = ask(
            "OpenSubtitles API key (optional, https://www.opensubtitles.com/consumers): "
        )
        if value:
            changes["OPENSUBTITLES_API_KEY"] = value
    if needs("sktorrent", "SKTORRENT_USERNAME", "SKTORRENT_PASSWORD"):
        print(
            "SkTorrent: Czech/Slovak films and series with dubbing "
            "(free account at https://sktorrent.eu)."
        )
        user = ask("  SkTorrent username (Enter = skip): ")
        if user:
            changes["SKTORRENT_USERNAME"] = user
            changes["SKTORRENT_PASSWORD"] = ask("  SkTorrent password: ", secret=True)
    # Local Whisper is set up by install; a Groq key is the optional, faster alternative.
    if (
        not env.get("TRANSCRIBE_API_URL")
        and not _whisper_ready()
        and needs("transcribe", "TRANSCRIBE_API_KEY")
    ):
        print(
            "AI subtitles from the audio: free Groq key from "
            "https://console.groq.com/keys (no card needed)."
        )
        value = ask("  Groq API key (Enter = skip): ", secret=True)
        if value:
            changes.update(
                TRANSCRIBE_API_KEY=value,
                TRANSCRIBE_PROVIDER="api",
                TRANSCRIBE_ENABLED="true",
            )

    os.makedirs(CACHE, exist_ok=True)
    with open(asked_file, "w", encoding="utf-8") as f:
        f.write("\n".join(sorted(asked)) + "\n")
    if changes:
        config.save_settings(changes)
        print(f"Saved to {ENV_FILE}.")
    return bool(changes)


def install() -> None:
    os.makedirs(LOGS, exist_ok=True)
    os.makedirs(os.path.join(REPO, "data", "torrserver"), exist_ok=True)
    _install_torrserver()
    _create_env()
    print(
        "Keys and logins can also be entered later on the web: Settings (login admin / admin)."
    )
    ask_keys()
    try:
        _install_whisper()
    except Exception as e:  # everything else works without it
        print(
            f"!! Local Whisper could not be installed ({e}) - a Groq key works too (Settings)."
        )


# --- commands ----------------------------------------------------------------


def _lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(
                ("192.0.2.1", 9)
            )  # no packet is sent; just picks the network address
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def _responds(url: str) -> bool:
    try:
        requests.get(url, timeout=3)
        return True
    except requests.RequestException:
        return False


def status() -> None:
    pid = _supervisor()
    port = _app_port()
    print(f"supervisor:  {f'running (pid {pid})' if pid else 'stopped'}")
    checks = {
        "app": f"http://127.0.0.1:{port}/api/healthz",
        "torrserver": "http://127.0.0.1:8090/echo",
    }
    if _whisper_ready():
        checks["whisper"] = f"http://127.0.0.1:{WHISPER_PORT}/"
    for name, url in checks.items():
        print(f"{name + ':':<12} {'ok' if _responds(url) else 'not responding'}")


def start() -> None:
    if not _supervisor():
        _launch_supervisor()
        port = _app_port()
        for _ in range(60):
            if _responds(f"http://127.0.0.1:{port}/api/healthz"):
                break
            time.sleep(1)
    status()
    port = _app_port()
    print()
    print(f"Open on this PC:      http://127.0.0.1:{port}")
    if (_env().get("HOST") or "0.0.0.0") == "0.0.0.0":
        print(f"Open on your network: http://{_lan_ip()}:{port}")


def stop() -> None:
    pid = _supervisor()
    if pid:
        _kill_tree(pid)
    _kill_leftovers()
    if os.path.exists(PID_FILE):
        os.remove(PID_FILE)


def update() -> None:
    from streamfinder.version import COMMIT, VERSION

    target = _new_version()
    if not target:
        if ask_keys() and _supervisor():
            stop()
            start()
        print(f"Already up to date ({VERSION} {COMMIT})")
        return
    print(f"==> Updating to {target if RELEASE else target[:7]}")
    stop()  # running files can't be replaced
    ok = _apply_update(target, interactive=True)
    if not RELEASE:  # the update helper starts the new version itself
        start()
    if not ok:
        print("!! The update failed - see the output above.")


def open_app() -> None:
    """Start (when stopped) and open the app in the browser."""
    if not _supervisor():
        start()
    webbrowser.open(f"http://127.0.0.1:{_app_port()}")


# --- the tray icon (StreamFinder.exe) ---------------------------------------

TRAY_TEXT = {
    "en": {
        "open": "Open StreamFinder",
        "autostart": "Start with Windows",
        "update": "Check for updates",
        "quit": "Quit",
        "up_to_date": "StreamFinder {version} is up to date.",
        "updating": "Installing the new version, StreamFinder restarts in a moment.",
        "update_failed": "The update failed - see cache/logs/supervisor.log.",
    },
    "cs": {
        "open": "Otevřít StreamFinder",
        "autostart": "Spouštět s Windows",
        "update": "Zkontrolovat aktualizace",
        "quit": "Ukončit",
        "up_to_date": "StreamFinder {version} je aktuální.",
        "updating": "Instaluji novou verzi, StreamFinder se za chvíli znovu spustí.",
        "update_failed": "Aktualizace selhala - viz cache/logs/supervisor.log.",
    },
}


def _tray_text() -> dict[str, str]:
    # Windows display language: Czech and Slovak get Czech.
    language = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
    return TRAY_TEXT["cs" if language in (0x05, 0x1B) else "en"]


def _tray_pid() -> int | None:
    try:
        with open(TRAY_PID_FILE, encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    image = _image(pid)
    return pid if image and os.path.basename(image).lower() == "pythonw.exe" else None


def _set_autostart(on: bool) -> None:
    """The Startup-folder shortcut that starts StreamFinder (without the browser) at logon."""
    if not on:
        if os.path.exists(STARTUP_LINK):
            os.remove(STARTUP_LINK)
        return
    if RELEASE:
        target, arguments, folder = LAUNCHER, "--background", ROOT
    else:
        target, arguments, folder = (
            PYTHONW,
            f'"{os.path.abspath(__file__)}" tray --background',
            REPO,
        )
    script = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps(STARTUP_LINK)}); "
        f"$s.TargetPath = {_ps(target)}; $s.Arguments = {_ps(arguments)}; "
        f"$s.WorkingDirectory = {_ps(folder)}; $s.IconLocation = {_ps(ICON)}; $s.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        creationflags=NO_WINDOW,
    )


def tray(*args: str) -> None:
    """StreamFinder.exe: start everything, open the browser and stay in the tray."""
    background = "--background" in args
    if _tray_pid():  # already running: just open it
        if not background:
            open_app()
        return
    import pystray
    from PIL import Image

    from streamfinder.version import VERSION

    os.makedirs(LOGS, exist_ok=True)
    logging.basicConfig(
        filename=os.path.join(LOGS, "tray.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    with open(TRAY_PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    text = _tray_text()
    try:
        install()  # first start: .env, Whisper settings; later: nothing to do
    except Exception:
        log.exception("install step failed")
    if not _supervisor():
        _launch_supervisor()

    def open_when_ready() -> None:
        port = _app_port()
        for _ in range(90):
            if _responds(f"http://127.0.0.1:{port}/api/healthz"):
                break
            time.sleep(1)
        webbrowser.open(f"http://127.0.0.1:{port}")

    def check_update(icon) -> None:
        target = _new_version()
        if not target:
            icon.notify(text["up_to_date"].format(version=VERSION), "StreamFinder")
            return
        icon.notify(text["updating"], "StreamFinder")
        stop()
        if not _apply_update(target, interactive=False):
            _launch_supervisor()
            icon.notify(text["update_failed"], "StreamFinder")
        elif (
            not RELEASE
        ):  # a clone: start the new version (the zip helper does it itself)
            os.remove(TRAY_PID_FILE)
            _launch_supervisor()
            _detach([PYTHONW, os.path.abspath(__file__), "tray", "--background"])
            icon.stop()

    def quit_app(icon) -> None:
        stop()
        icon.stop()

    in_background = lambda job: (
        lambda icon, item: threading.Thread(  # noqa: E731
            target=job, args=(icon,), daemon=True
        ).start()
    )
    menu = pystray.Menu(
        pystray.MenuItem(
            text["open"], in_background(lambda icon: open_app()), default=True
        ),
        pystray.MenuItem(
            text["autostart"],
            lambda icon, item: _set_autostart(not os.path.exists(STARTUP_LINK)),
            checked=lambda item: os.path.exists(STARTUP_LINK),
        ),
        pystray.MenuItem(text["update"], in_background(check_update)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(text["quit"], lambda icon, item: quit_app(icon)),
    )
    icon = pystray.Icon(
        "StreamFinder", Image.open(ICON), f"StreamFinder {VERSION}", menu
    )
    if not background:
        threading.Thread(target=open_when_ready, daemon=True).start()
    try:
        icon.run()
    finally:
        if _tray_pid() == os.getpid():
            os.remove(TRAY_PID_FILE)


def logs() -> None:
    """The last lines of every log, then follow them (Ctrl+C to stop)."""
    files = (
        sorted(os.path.join(LOGS, n) for n in os.listdir(LOGS) if n.endswith(".log"))
        if os.path.isdir(LOGS)
        else []
    )
    positions = {}
    for path in files:
        with open(path, "rb") as f:
            data = f.read()
        positions[path] = len(data)
        print(f"==> {os.path.basename(path)}")
        print("\n".join(data.decode("utf-8", "replace").splitlines()[-30:]))
    try:
        while True:
            time.sleep(1)
            for path in files:
                size = os.path.getsize(path) if os.path.exists(path) else 0
                if size < positions[path]:
                    positions[path] = 0  # rotated
                if size > positions[path]:
                    with open(path, "rb") as f:
                        f.seek(positions[path])
                        data = f.read()
                    positions[path] += len(data)
                    name = os.path.basename(path)[:-4]
                    for line in data.decode("utf-8", "replace").splitlines():
                        print(f"[{name}] {line}")
    except KeyboardInterrupt:
        pass


COMMANDS = {
    "start": start,
    "stop": stop,
    "restart": lambda: (stop(), start()),
    "status": status,
    "update": update,
    "logs": logs,
    "open": open_app,
    "tray": tray,  # StreamFinder.exe and the logon shortcut
    "install": install,  # windows-install.ps1 and updates
    "run": run,  # the supervisor (logon shortcut)
}

if __name__ == "__main__":
    if sys.stdout:  # the console code page can't show every character in the logs
        sys.stdout.reconfigure(errors="replace")
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command not in COMMANDS:
        print("usage: streamfinder start|stop|restart|status|update|logs")
        sys.exit(1)
    COMMANDS[command](*sys.argv[2:])
