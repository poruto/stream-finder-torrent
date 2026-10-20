"""Settings file, admin login, translations and version numbering."""

import os
import re
import string
from pathlib import Path

import pytest
from dotenv import dotenv_values
from flask import Flask

from streamfinder import auth, config, i18n, version
from streamfinder.subtitles.whisper_models import MODELS as WHISPER_MODELS
from streamfinder.version import from_subjects
from streamfinder.views import BROWSE_LANGUAGES, BROWSE_SORTS

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "streamfinder"


# -- the settings file -----------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """save_settings() writes into a temporary file; os.environ and config are restored."""
    path = tmp_path / "data" / "settings.env"
    monkeypatch.setattr(config, "SETTINGS_FILE", str(path))
    for key in ("SF_TEST_A", "SF_TEST_B", "SF_TEST_C"):
        monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(config, key, None, raising=False)
    return path


def test_save_settings_creates_the_file_and_applies_values(settings):
    config.save_settings({"SF_TEST_A": "one"})
    assert settings.read_text(encoding="utf-8") == "SF_TEST_A=one\n"
    assert os.environ["SF_TEST_A"] == "one" and config.SF_TEST_A == "one"


def test_save_settings_keeps_other_lines_and_replaces_in_place(settings):
    settings.parent.mkdir()
    settings.write_text("# comment\nSF_TEST_A=old\nOTHER=keep\n", encoding="utf-8")
    config.save_settings({"SF_TEST_A": "new", "SF_TEST_B": "added"})
    assert settings.read_text(encoding="utf-8").splitlines() == [
        "# comment",
        "SF_TEST_A=new",
        "OTHER=keep",
        "SF_TEST_B=added",
    ]


def test_save_settings_can_clear_a_value(settings):
    config.save_settings({"SF_TEST_A": "x"})
    config.save_settings({"SF_TEST_A": ""})
    assert dotenv_values(settings)["SF_TEST_A"] == ""


def test_save_settings_leaves_no_temporary_file(settings):
    config.save_settings({"SF_TEST_A": "x"})
    assert [p.name for p in settings.parent.iterdir()] == ["settings.env"]


@pytest.mark.parametrize(
    "value",
    [
        "plain",
        "gsk_abc123-XYZ",
        "http://127.0.0.1:8178/v1",
        "user@example.com",
        "scrypt:32768:8:1$salt$0123abcd",  # an admin password hash
        "with space",
        "has#hash",
        'double"quote',
        "back\\slash",
        " leading space",
        "a=b",
        "",
    ],
)
def test_saved_values_read_back_unchanged(settings, value):
    config.save_settings({"SF_TEST_A": value})
    assert dotenv_values(settings)["SF_TEST_A"] == value


def test_apostrophe_in_a_password_survives_a_restart(settings):
    config.save_settings({"SF_TEST_A": "it's-secret"})
    assert dotenv_values(settings)["SF_TEST_A"] == "it's-secret"


def test_dollar_brace_in_a_password_survives_a_restart(settings, monkeypatch):
    monkeypatch.setenv("SF_TEST_B", "expanded")
    config.save_settings({"SF_TEST_A": "pa${SF_TEST_B}ss"})
    # Read as the app reads it (config.py: no ${VAR} expansion).
    assert dotenv_values(settings, interpolate=False)["SF_TEST_A"] == "pa${SF_TEST_B}ss"


@pytest.mark.parametrize(
    "value, on",
    [
        (True, True),
        ("true", True),
        ("1", True),
        (" Yes ", True),
        ("on", True),
        (False, False),
        ("false", False),
        ("", False),
        ("0", False),
        (None, False),
    ],
)
def test_flag(value, on):
    assert config.flag(value) is on


def test_env_helpers(monkeypatch):
    monkeypatch.setenv("SF_TEST_INT", "nope")
    assert config._env_int("SF_TEST_INT", 7) == 7
    monkeypatch.setenv("SF_TEST_INT", "12")
    assert config._env_int("SF_TEST_INT", 7) == 12
    monkeypatch.setenv("SF_TEST_LIST", " a, ,b ,")
    assert config._env_list("SF_TEST_LIST", "x") == ["a", "b"]
    monkeypatch.delenv("SF_TEST_LIST")
    assert config._env_list("SF_TEST_LIST", "cs,en") == ["cs", "en"]
    monkeypatch.setenv("SF_TEST_BOOL", "Yes")
    assert config._env_bool("SF_TEST_BOOL", False)
    assert not config._env_bool("SF_TEST_MISSING", False)


def test_every_editable_key_is_a_config_setting():
    for key in config.EDITABLE_KEYS:
        assert hasattr(config, key), key


# -- admin login -------------------------------------------------------------------------------


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USERNAME", "")
    monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "")
    monkeypatch.setattr(auth.time, "sleep", lambda _: None)
    auth._failures.clear()
    app = Flask(__name__)
    app.secret_key = "test"
    yield app
    auth._failures.clear()


def test_default_account_until_changed(admin, monkeypatch):
    assert auth.using_default() and auth.username() == "admin"
    assert auth.verify("admin", "admin")
    assert not auth.verify("admin", "Admin") and not auth.verify("root", "admin")
    from werkzeug.security import generate_password_hash

    monkeypatch.setattr(config, "ADMIN_USERNAME", "boss")
    monkeypatch.setattr(
        config, "ADMIN_PASSWORD_HASH", generate_password_hash("s3cret-pass")
    )
    assert not auth.using_default() and auth.username() == "boss"
    assert auth.verify("boss", "s3cret-pass")
    assert not auth.verify("admin", "admin")  # the default stops working
    assert not auth.verify("boss", "wrong")


def test_login_sets_the_session_and_logout_clears_it(admin):
    with admin.test_request_context("/", environ_base={"REMOTE_ADDR": "10.0.0.1"}):
        assert auth.login("admin", "admin") == (True, None)
        assert auth.is_admin()
        auth.logout()
        assert not auth.is_admin()


def test_lockout_is_per_client_and_ends(admin, monkeypatch):
    for _ in range(auth.MAX_FAILURES):
        with admin.test_request_context("/", environ_base={"REMOTE_ADDR": "10.0.0.1"}):
            ok, error = auth.login("admin", "wrong")
            assert not ok and error
    assert auth.locked_for("10.0.0.1") > 0
    with admin.test_request_context("/", environ_base={"REMOTE_ADDR": "10.0.0.1"}):
        ok, error = auth.login("admin", "admin")
        assert not ok
    # Another device is not locked out.
    with admin.test_request_context("/", environ_base={"REMOTE_ADDR": "10.0.0.2"}):
        assert auth.login("admin", "admin")[0]
    # After the lockout the right password works and resets the counter.
    later = auth.time.time() + auth.LOCKOUT + 1
    monkeypatch.setattr(auth.time, "time", lambda: later)
    assert auth.locked_for("10.0.0.1") == 0
    with admin.test_request_context("/", environ_base={"REMOTE_ADDR": "10.0.0.1"}):
        assert auth.login("admin", "admin")[0]
    assert "10.0.0.1" not in auth._failures


def test_a_few_wrong_passwords_do_not_lock(admin):
    for _ in range(auth.MAX_FAILURES - 1):
        with admin.test_request_context("/", environ_base={"REMOTE_ADDR": "10.0.0.3"}):
            auth.login("admin", "wrong")
    assert auth.locked_for("10.0.0.3") == 0


def test_admin_required_decorator(admin):
    @auth.admin_required
    def secret():
        return "ok"

    with admin.test_request_context("/"):
        response, status = secret()
        assert status == 401 and response.get_json()["success"] is False
    with admin.test_request_context("/"):
        auth.login("admin", "admin")
        assert secret() == "ok"


def test_change_credentials_stores_only_a_hash(admin, tmp_path, monkeypatch):
    path = tmp_path / "settings.env"
    monkeypatch.setattr(config, "SETTINGS_FILE", str(path))
    for key in ("ADMIN_USERNAME", "ADMIN_PASSWORD_HASH"):
        monkeypatch.delenv(key, raising=False)
    auth.change_credentials("boss", "correct horse $1")
    stored = dotenv_values(path)
    assert stored["ADMIN_USERNAME"] == "boss"
    assert "correct horse" not in path.read_text(encoding="utf-8")
    assert auth.verify("boss", "correct horse $1")
    # The stored hash still verifies after reading the file back (a restart).
    monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", stored["ADMIN_PASSWORD_HASH"])
    assert auth.verify("boss", "correct horse $1")


# -- translations ------------------------------------------------------------------------------

EN, CS = i18n.STRINGS["en"], i18n.STRINGS["cs"]


def _placeholders(text):
    return sorted(
        {name for _, name, _, _ in string.Formatter().parse(text) if name is not None}
    )


def test_every_key_exists_in_both_languages():
    assert sorted(set(EN) - set(CS)) == []
    assert sorted(set(CS) - set(EN)) == []


def test_placeholders_are_identical_in_both_languages():
    different = {
        key: (_placeholders(EN[key]), _placeholders(CS[key]))
        for key in EN
        if key in CS and _placeholders(EN[key]) != _placeholders(CS[key])
    }
    assert different == {}


def test_no_string_is_empty_or_unformattable():
    for lang, strings in i18n.STRINGS.items():
        for key, text in strings.items():
            assert text.strip(), (lang, key)
            text.format(**{name: "x" for name in _placeholders(text)})


def test_czech_strings_are_translated():
    # Long sentences that are word for word the English one were forgotten.
    same = [k for k in EN if len(EN[k]) > 25 and " " in EN[k] and EN[k] == CS[k]]
    assert same == []


def test_plurals_have_every_form():
    bases = {k.rsplit("_", 1)[0] for k in EN if k.endswith(("_one", "_few", "_many"))}
    for base in bases:
        for form in ("one", "few", "many"):
            assert f"{base}_{form}" in EN and f"{base}_{form}" in CS, (base, form)


def _used_keys():
    """Literal t("key") / SF.t("key") / plural(n, "key") uses in templates, Python and JS."""
    keys, plurals = set(), set()
    files = [
        *PACKAGE.glob("templates/*.html"),
        *PACKAGE.glob("**/*.py"),
        *PACKAGE.glob("static/js/**/*.js"),
    ]
    for path in files:
        text = path.read_text(encoding="utf-8")
        keys |= set(re.findall(r"""(?<![\w.])t\(\s*["']([a-z0-9_]+)["']""", text))
        keys |= set(re.findall(r"""SF\.t\(\s*["']([a-z0-9_]+)["']""", text))
        plurals |= set(re.findall(r"""plural\([^,()]+,\s*["']([a-z0-9_]+)["']""", text))
    return keys, plurals


def test_every_key_used_in_the_code_exists():
    keys, plurals = _used_keys()
    assert len(keys) > 100  # the scan finds the code's translations
    # Keys ending in "_" are prefixes completed at runtime (t('sort_' ~ key)).
    assert sorted(k for k in keys if k not in EN and not k.endswith("_")) == []
    for base in plurals:
        assert f"{base}_one" in EN and f"{base}_many" in EN, base


@pytest.mark.parametrize(
    "prefix, suffixes",
    [
        ("note_", ["hash", "machine", "hi", "torrent"]),  # SubtitleCandidate.note
        ("pack_", ["season", "multi", "complete"]),  # pack_kind()
        ("avail_", ["excellent", "good", "weak", "none"]),  # TorrentResult.availability
        ("ts_", ["offline", "timeout", "error", "no_video"]),  # TorrServerError codes
        ("sort_", list(BROWSE_SORTS)),  # browse.html
        ("lang_", list(BROWSE_LANGUAGES)),  # browse.html
        ("whisper_quality_", list(WHISPER_MODELS)),  # settings.html
    ],
)
def test_keys_built_at_runtime_exist(prefix, suffixes):
    missing = [prefix + s for s in suffixes if prefix + s not in EN]
    assert missing == []


# Keys put together at runtime, which no scan for a quoted key finds.
RUNTIME_KEYS = [
    r"health_group_\w+",  # health.py: t(f"health_group_{g}")
    r"sort_\w+",  # browse.html: t('sort_' ~ key)
    r"lang_\w+",  # browse.html: t('lang_' ~ code); player.js: `lang_${kind}`
    r"quality_\w+",  # settings.html, _player.html: t('quality_' ~ value [~ '_text'])
    r"version_lang_\w+",  # settings.html: t('version_lang_' ~ value [~ '_text'])
    r"subs_missing_\w+",  # settings.html
    r"translator_\w+",  # settings.html; player.js: `translator_${...}`
    r"ai_via_\w+",  # settings.html
    r"whisper_quality_\w+",  # settings.html
    r"read_ahead_\w+",  # settings.html
    r"ts_\w+",  # api_helpers.torrserver_error: t(f"ts_{error.code}")
    r"(avail|pack|note|engine|subs_source|versions_group)_\w+",  # player.js: `avail_${a}`
    r"key_remove_confirm_\w+",  # app.js: `key_remove_confirm_${what}`
]


def _code_files():
    return [
        path
        for path in [
            *PACKAGE.rglob("*.py"),
            *PACKAGE.rglob("templates/*.html"),
            *PACKAGE.rglob("static/js/**/*.js"),
        ]
        if path.name != "i18n.py"
    ]


def test_every_key_is_used_somewhere():
    used, plurals = set(), set()
    for path in _code_files():
        text = path.read_text(encoding="utf-8")
        used |= set(
            re.findall(r"""["']([a-z0-9_]+)["']""", text)
        )  # t("key"), keys in lists
        plurals |= set(re.findall(r"""plural\([^,()]+,\s*["']([a-z0-9_]+)["']""", text))
    runtime = re.compile("|".join(RUNTIME_KEYS))
    unused = [
        key
        for key in EN
        if key not in used
        and not runtime.fullmatch(key)
        and key.rsplit("_", 1)[0] not in plurals
    ]
    assert unused == []


def _js_references():
    """Keys the scripts ask for: t("key") (also in ternaries), plural forms, `prefix_${}`."""
    keys = set()
    for path in PACKAGE.rglob("static/js/**/*.js"):
        text = path.read_text(encoding="utf-8")
        for call in re.findall(r"""(?<![\w.])(?:SF\.)?t\(([^()]*)""", text):
            keys |= set(re.findall(r"""["']([a-z0-9_]+)["']""", call))
        for base in re.findall(
            r"""plural\([^;]*?,\s*["']([a-z0-9_]+)["']\s*\)""", text
        ):
            keys |= {f"{base}_{form}" for form in ("one", "few", "many")}
        for prefix in re.findall(r"""t\(\s*`([a-z0-9_]+_)\$\{""", text):
            keys |= {key for key in EN if key.startswith(prefix)}
    return keys


def test_pages_send_the_keys_the_scripts_use():
    sent = i18n.js_keys()
    referenced = _js_references()
    assert len(referenced) > 150  # the scan finds the scripts' translations
    assert sorted(k for k in referenced if k in EN and k not in sent) == []
    # Keys the scripts get from elsewhere: the library's list, the server's notice.
    for key in (
        "library_cache_other",
        "library_whisper",
        "pick_recommended",
        *i18n.JS_DYNAMIC_KEYS,
    ):
        assert key in sent
    # Templates translate the rest on the server: pages carry well under half of them.
    assert len(sent) < 0.6 * len(EN)
    assert "settings_title" not in sent and "footer_data" not in sent


def test_js_strings_are_the_sent_keys_in_the_language():
    app = Flask(__name__)
    with app.test_request_context("/", headers={"Cookie": "lang=cs"}):
        strings = i18n.js_strings()
    assert strings.keys() == i18n.js_keys()
    assert strings["key_saved"] == CS["key_saved"]


def test_t_uses_the_cookie_language_and_falls_back():
    app = Flask(__name__)
    with app.test_request_context("/", headers={"Cookie": "lang=cs"}):
        assert i18n.current_language() == "cs" and i18n.tmdb_locale() == "cs-CZ"
        assert i18n.t("nav_movies") == CS["nav_movies"]
        assert i18n.t("no_such_key") == "no_such_key"
        assert i18n.js_strings()["key_saved"] == CS["key_saved"]
    with app.test_request_context("/", headers={"Cookie": "lang=xx"}):
        assert (
            i18n.current_language() == "en" and i18n.t("nav_movies") == EN["nav_movies"]
        )
    assert i18n.current_language() == "en"  # outside a request


@pytest.mark.parametrize(
    "n, form",
    [(1, "one"), (2, "few"), (4, "few"), (5, "many"), (0, "many"), (21, "many")],
)
def test_plural_forms(n, form):
    app = Flask(__name__)
    with app.test_request_context("/", headers={"Cookie": "lang=cs"}):
        assert i18n.plural(n, "seasons") == CS[f"seasons_{form}"].format(n=n)


# -- version numbers ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "subjects, expected",
    [
        ([], "1.0.0"),
        (["fix: a"], "1.0.1"),
        (["feat: a"], "1.1.0"),
        (["feat(ui): a", "fix(api): b", "fix: c"], "1.1.2"),
        (["feat!: a"], "2.0.0"),
        (["fix(scope)!: breaking fix", "feat: x"], "2.1.0"),
        (["Merge branch 'x'", 'Revert "feat: a"'], "1.0.2"),
        (["feature: not conventional"], "1.0.1"),
        (["feat:no space"], "1.1.0"),
        (["FEAT: shouting"], "1.0.1"),
    ],
)
def test_from_subjects(subjects, expected):
    assert from_subjects(subjects) == expected


def test_version_from_environment_or_file(monkeypatch, tmp_path):
    monkeypatch.setenv("STREAMFINDER_VERSION", "9.8.7")
    monkeypatch.setenv("STREAMFINDER_COMMIT", "abc1234")
    assert version._read() == ("9.8.7", "abc1234")
    monkeypatch.delenv("STREAMFINDER_VERSION")
    monkeypatch.setattr(version, "BASE_DIR", str(tmp_path))
    (tmp_path / "VERSION").write_text("1.12.3 a6d1e11\n", encoding="utf-8")
    assert version._read() == ("1.12.3", "a6d1e11")
    (tmp_path / "VERSION").write_text("1.12.3", encoding="utf-8")
    assert version._read() == ("1.12.3", "")


def test_version_without_git_is_dev(monkeypatch, tmp_path):
    monkeypatch.delenv("STREAMFINDER_VERSION", raising=False)
    monkeypatch.setattr(version, "BASE_DIR", str(tmp_path))  # no VERSION file, no .git

    def no_git(*args):
        raise FileNotFoundError("git")

    monkeypatch.setattr(version, "_git", no_git)
    assert version._read() == ("dev", "")


def test_shallow_clone_cannot_count_commits(monkeypatch):
    answers = {
        ("rev-parse", "--short=7", "HEAD"): "abc1234\n",
        ("rev-parse", "--is-shallow-repository"): "true\n",
    }
    monkeypatch.setattr(version, "_git", lambda *args: answers.get(args, ""))
    assert version._from_git() == ("dev", "abc1234")
    answers[("rev-parse", "--is-shallow-repository")] = "false\n"
    answers[("log", "--reverse", "--format=%s")] = "chore: init\nfeat: one\nfix: two\n"
    assert version._from_git() == ("1.1.1", "abc1234")
