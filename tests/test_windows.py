"""windows/control.py: the Windows supervisor's pure helpers (Windows only)."""

import importlib.util
import io
import os
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only module")

CONTROL = Path(__file__).resolve().parent.parent / "windows" / "control.py"


@pytest.fixture(scope="module")
def control():
    spec = importlib.util.spec_from_file_location("sf_windows_control", CONTROL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sandbox(control, tmp_path, monkeypatch):
    """Point every path of control.py into a temporary folder."""
    cache = tmp_path / "cache"
    monkeypatch.setattr(control, "REPO", str(tmp_path))
    monkeypatch.setattr(control, "CACHE", str(cache))
    monkeypatch.setattr(control, "ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setattr(control, "FAILED_FILE", str(cache / ".update-failed"))
    monkeypatch.setattr(control, "PID_FILE", str(cache / "streamfinder.pid"))
    monkeypatch.setattr(
        control, "WHISPER_SERVER", str(tmp_path / "whisper" / "whisper-server.exe")
    )
    monkeypatch.setattr(control, "WHISPER_MODELS", str(tmp_path / "whisper" / "models"))
    return tmp_path


# -- versions and updates ------------------------------------------------------------------


@pytest.mark.parametrize(
    "version, key",
    [
        ("1.12.3", (1, 12, 3)),
        ("2.0", (2, 0)),
        ("1.2.3-rc1", (1, 2)),
        ("dev", ()),
        ("", ()),
    ],
)
def test_version_key(control, version, key):
    assert control._version_key(version) == key


def test_version_key_compares_numbers_not_text(control):
    assert control._version_key("1.10.0") > control._version_key("1.9.9")
    assert control._version_key("2.0.0") > control._version_key("1.99.99")
    assert control._version_key("1.0.0") > control._version_key("dev")


class Release:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def _release(tag):
    return {
        "tag_name": tag,
        "assets": [
            {
                "name": "checksums.txt",
                "browser_download_url": "https://dl/checksums.txt",
            },
            {
                "name": f"StreamFinder-{tag}-Windows.zip",
                "browser_download_url": f"https://dl/{tag}.zip",
            },
        ],
    }


@pytest.fixture
def running(monkeypatch):
    from streamfinder import version

    monkeypatch.setattr(version, "VERSION", "1.12.3")


def test_newer_release_is_offered(control, sandbox, running, monkeypatch):
    monkeypatch.setattr(
        control.requests, "get", lambda *a, **k: Release(_release("v1.13.0"))
    )
    assert control._new_release() == "https://dl/v1.13.0.zip"


@pytest.mark.parametrize("tag", ["v1.12.3", "v1.12.2", "1.9.99"])
def test_same_or_older_release_is_ignored(control, sandbox, running, monkeypatch, tag):
    monkeypatch.setattr(control.requests, "get", lambda *a, **k: Release(_release(tag)))
    assert control._new_release() is None


def test_release_that_failed_before_is_not_retried(
    control, sandbox, running, monkeypatch
):
    monkeypatch.setattr(
        control.requests, "get", lambda *a, **k: Release(_release("v2.0.0"))
    )
    os.makedirs(control.CACHE)
    Path(control.FAILED_FILE).write_text("https://dl/v2.0.0.zip\n", encoding="utf-8")
    assert control._failed_before("https://dl/v2.0.0.zip")
    assert control._new_release() is None
    monkeypatch.setattr(
        control.requests, "get", lambda *a, **k: Release(_release("v2.0.1"))
    )
    assert control._new_release() == "https://dl/v2.0.1.zip"


def test_release_without_a_windows_zip_or_unreachable(
    control, sandbox, running, monkeypatch
):
    monkeypatch.setattr(
        control.requests,
        "get",
        lambda *a, **k: Release({"tag_name": "v9.0.0", "assets": []}),
    )
    assert control._new_release() is None

    def offline(*args, **kwargs):
        raise control.requests.ConnectionError("offline")

    monkeypatch.setattr(control.requests, "get", offline)
    assert control._new_release() is None


def test_failed_before_without_a_file(control, sandbox):
    assert not control._failed_before("anything")


# -- configuration -------------------------------------------------------------------------


def test_create_env_from_the_example(control, sandbox):
    (sandbox / ".env.example").write_text(
        "# keys\nTMDB_API_KEY=\nSECRET_KEY=\nHOST=127.0.0.1\nPORT=5000\n",
        encoding="utf-8",
    )
    control._create_env()
    lines = (sandbox / ".env").read_text(encoding="utf-8").splitlines()
    secret = next(line for line in lines if line.startswith("SECRET_KEY="))
    assert len(secret.split("=", 1)[1]) == 64
    assert "HOST=0.0.0.0" in lines  # phones and TVs reach it
    assert "# keys" in lines and "PORT=5000" in lines


def test_create_env_never_overwrites(control, sandbox):
    (sandbox / ".env").write_text("TMDB_API_KEY=mine\n", encoding="utf-8")
    control._create_env()
    assert (sandbox / ".env").read_text(encoding="utf-8") == "TMDB_API_KEY=mine\n"


def test_create_env_secret_differs_each_install(control, sandbox):
    (sandbox / ".env.example").write_text("SECRET_KEY=\n", encoding="utf-8")
    control._create_env()
    first = (sandbox / ".env").read_text(encoding="utf-8")
    os.remove(sandbox / ".env")
    control._create_env()
    assert (sandbox / ".env").read_text(encoding="utf-8") != first


@pytest.mark.parametrize(
    "port, expected", [("8080", 8080), ("", 5000), ("abc", 5000), (None, 5000)]
)
def test_app_port(control, sandbox, port, expected):
    if port is not None:
        (sandbox / ".env").write_text(f"PORT={port}\n", encoding="utf-8")
    assert control._app_port() == expected


def test_ask_keys_skips_without_a_terminal(control, sandbox, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert control.ask_keys() is False
    assert not os.path.exists(control.CACHE)  # nothing written, nothing remembered


def test_ask_keys_skips_when_stdin_is_missing(control, sandbox, monkeypatch):
    monkeypatch.setattr(sys, "stdin", None)  # pythonw has no console
    assert control.ask_keys() is False


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def terminal(control, sandbox, monkeypatch):
    from streamfinder import config

    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr(config, "SETTINGS_FILE", control.ENV_FILE)
    for key in (
        "TMDB_API_KEY",
        "OPENSUBTITLES_API_KEY",
        "SKTORRENT_USERNAME",
        "SKTORRENT_PASSWORD",
        "TRANSCRIBE_API_KEY",
        "TRANSCRIBE_PROVIDER",
        "TRANSCRIBE_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(config, key, "")
    monkeypatch.setattr(control, "_whisper_ready", lambda: False)
    asked = []

    def answer(prompts):
        answers = iter(prompts)

        def ask(prompt=""):
            asked.append(prompt)
            return next(answers, "")

        monkeypatch.setattr("builtins.input", ask)
        monkeypatch.setattr("getpass.getpass", ask)

    return answer, asked


def test_ask_keys_saves_answers_and_never_asks_twice(control, terminal, capsys):
    answer, asked = terminal
    # TMDB, OpenSubtitles (skipped), SkTorrent user + password, Groq key.
    answer(["tmdb-'key", "", "petr", "heslo", "gsk_abc"])
    assert control.ask_keys() is True
    from dotenv import dotenv_values

    saved = dotenv_values(control.ENV_FILE)
    assert saved["TMDB_API_KEY"] == "tmdb-key"  # quotes are dropped on input
    assert (
        saved["SKTORRENT_USERNAME"] == "petr" and saved["SKTORRENT_PASSWORD"] == "heslo"
    )
    assert (
        saved["TRANSCRIBE_PROVIDER"] == "api" and saved["TRANSCRIBE_ENABLED"] == "true"
    )
    assert "OPENSUBTITLES_API_KEY" not in saved
    count = len(asked)
    answer([])
    assert control.ask_keys() is False
    assert len(asked) == count  # skipped and answered questions are not asked again


def test_ask_keys_skipping_everything_saves_nothing(control, terminal):
    answer, asked = terminal
    answer([])
    assert control.ask_keys() is False
    assert not os.path.exists(control.ENV_FILE)
    remembered = (
        (Path(control.CACHE) / ".setup-asked").read_text(encoding="utf-8").split()
    )
    assert set(remembered) == {"tmdb", "opensubtitles", "sktorrent", "transcribe"}


def test_ask_keys_does_not_ask_for_what_is_set(control, terminal, sandbox):
    (sandbox / ".env").write_text(
        "TMDB_API_KEY=x\nOPENSUBTITLES_API_KEY=y\nSKTORRENT_USERNAME=u\nSKTORRENT_PASSWORD=p\n"
        "TRANSCRIBE_API_URL=http://127.0.0.1:8178/v1\n",
        encoding="utf-8",
    )
    answer, asked = terminal
    answer([])
    assert control.ask_keys() is False and asked == []


# -- services ----------------------------------------------------------------------------------


def test_services_without_whisper(control, sandbox):
    (sandbox / ".env").write_text("HOST=0.0.0.0\nPORT=8000\n", encoding="utf-8")
    services = control._services()
    assert set(services) == {"torrserver", "app"}
    torrserver, app = services["torrserver"], services["app"]
    assert torrserver[0] == control.TORRSERVER and torrserver[1:3] == ["-p", "8090"]
    assert torrserver[torrserver.index("-d") + 1] == os.path.join(
        str(sandbox), "data", "torrserver"
    )
    assert app[:3] == [control.PYTHON, "-m", "waitress"]
    assert "--listen=0.0.0.0:8000" in app and app[-1] == "app:app"


def test_services_listen_everywhere_by_default(control, sandbox):
    assert "--listen=0.0.0.0:5000" in control._services()["app"]


def _install_whisper(sandbox, model="tiny-q5_1"):
    models = sandbox / "whisper" / "models"
    models.mkdir(parents=True)
    (sandbox / "whisper" / "whisper-server.exe").write_bytes(b"")
    (models / f"ggml-{model}.bin").write_bytes(b"")
    (models / "current.txt").write_text(model, encoding="utf-8")


def test_services_with_local_whisper(control, sandbox, monkeypatch):
    _install_whisper(sandbox, "base-q5_1")
    monkeypatch.setenv("WHISPER_THREADS", "3")
    whisper = control._services()["whisper"]
    assert whisper[0] == control.WHISPER_SERVER
    assert whisper[whisper.index("-m") + 1].endswith("ggml-base-q5_1.bin")
    assert whisper[whisper.index("--port") + 1] == str(control.WHISPER_PORT)
    assert whisper[whisper.index("--host") + 1] == "127.0.0.1"  # never on the network
    assert whisper[whisper.index("-t") + 1] == "3"
    assert whisper[whisper.index("--dtw") + 1] == "base"  # the model's own DTW preset
    assert whisper[whisper.index("--inference-path") + 1] == "/v1/audio/transcriptions"


def test_whisper_can_be_switched_off(control, sandbox):
    _install_whisper(sandbox)
    (sandbox / "cache").mkdir()
    (sandbox / "cache" / ".no-whisper").write_text("", encoding="utf-8")
    assert "whisper" not in control._services()


def test_whisper_without_its_model_is_not_started(control, sandbox):
    _install_whisper(sandbox)
    os.remove(sandbox / "whisper" / "models" / "ggml-tiny-q5_1.bin")
    assert control._whisper_model() is None and not control._whisper_ready()
    assert "whisper" not in control._services()


def test_whisper_threads_default_to_half_the_cores(control, monkeypatch):
    monkeypatch.delenv("WHISPER_THREADS", raising=False)
    monkeypatch.setattr(control.os, "cpu_count", lambda: 12)
    command = control._whisper_command("small-q5_1")
    assert command[command.index("-t") + 1] == "6"
    monkeypatch.setattr(control.os, "cpu_count", lambda: 32)
    command = control._whisper_command("small-q5_1")
    assert command[command.index("-t") + 1] == "8"
    monkeypatch.setattr(control.os, "cpu_count", lambda: None)
    command = control._whisper_command("small-q5_1")
    assert command[command.index("-t") + 1] == "2"


# -- processes ---------------------------------------------------------------------------------


def test_pid_file_garbage_is_empty(control, sandbox):
    assert control._pids() == {}
    os.makedirs(control.CACHE)
    Path(control.PID_FILE).write_text("{not json", encoding="utf-8")
    assert control._pids() == {}
    Path(control.PID_FILE).write_text(
        '{"supervisor": 1234, "app": 5678}', encoding="utf-8"
    )
    assert control._pids() == {"supervisor": 1234, "app": 5678}


def test_write_pids_includes_the_supervisor(control, sandbox):
    os.makedirs(control.CACHE)

    class Proc:
        pid = 42

    control._write_pids({"app": Proc()})
    assert control._pids() == {"supervisor": os.getpid(), "app": 42}
    assert not os.path.exists(control.PID_FILE + ".tmp")


def test_image_of_this_process_and_of_no_process(control):
    image = control._image(os.getpid())
    assert image and image.lower().endswith(".exe") and os.path.exists(image)
    assert control._image(0x7FFFFFF0) is None


def test_supervisor_pid_must_be_ours(control, sandbox):
    os.makedirs(control.CACHE)
    Path(control.PID_FILE).write_text(
        f'{{"supervisor": {os.getpid()}}}', encoding="utf-8"
    )
    # This test runs in python.exe, not the supervisor's pythonw.exe: not ours.
    assert control._supervisor() is None


def test_powershell_quoting(control):
    assert (
        control._ps(r"C:\Program Files\It's here") == r"'C:\Program Files\It''s here'"
    )


def test_tray_texts_exist_in_both_languages(control):
    assert set(control.TRAY_TEXT["en"]) == set(control.TRAY_TEXT["cs"])
    for key, text in control.TRAY_TEXT["en"].items():
        assert ("{version}" in text) == ("{version}" in control.TRAY_TEXT["cs"][key])


def test_command_names(control):
    assert set(control.COMMANDS) >= {
        "start",
        "stop",
        "restart",
        "status",
        "update",
        "logs",
        "tray",
        "install",
        "run",
    }


def test_app_window_is_a_browser_in_app_mode(control):
    command = control.window_command("C:/Edge/msedge.exe", "http://127.0.0.1:5000")
    assert command[0] == "C:/Edge/msedge.exe"
    assert "--app=http://127.0.0.1:5000" in command
    assert (
        f"--user-data-dir={control.WINDOW_PROFILE}" in command
    )  # own window size, no extensions
    assert "--no-first-run" in command


def test_app_window_falls_back_to_the_default_browser(control, monkeypatch):
    opened = []
    monkeypatch.setattr(control, "_browsers", list)
    monkeypatch.setattr(control.webbrowser, "open", opened.append)
    control.open_window("http://127.0.0.1:5000")
    assert opened == ["http://127.0.0.1:5000"]


def test_app_window_prefers_edge_then_chrome(control, monkeypatch):
    launched = []
    monkeypatch.setattr(control, "_browsers", lambda: ["edge.exe", "chrome.exe"])
    monkeypatch.setattr(control, "_detach", launched.append)
    control.open_window("http://127.0.0.1:5000")
    assert len(launched) == 1 and launched[0][0] == "edge.exe"
