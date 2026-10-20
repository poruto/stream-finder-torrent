"""Pure helpers of static/js/app.js, run with node (skipped when node is missing)."""

import json
import re
import shutil
import string
import subprocess
from pathlib import Path

import pytest

from streamfinder import i18n

APP_JS = (
    Path(__file__).resolve().parent.parent / "streamfinder" / "static" / "js" / "app.js"
)
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _snippet(pattern: str) -> str:
    match = re.search(
        pattern, APP_JS.read_text(encoding="utf-8"), re.DOTALL | re.MULTILINE
    )
    if not match:
        pytest.skip(f"app.js changed: {pattern!r} not found")
    return match.group(0)


def run_js(strings: dict, body: str):
    """Run `body` (which sets `out`) next to the extracted helpers; returns `out`."""
    helpers = "\n".join(
        [
            _snippet(r"^\s*SF\.t = .*?\n\s*const t = SF\.t;"),
            _snippet(r"^\s*const FINISHED = [\d.]+;\s*\n\s*SF\.isFinished = [^\n]+"),
            _snippet(r"^\s*const englishUrl = \(url\) =>.*?;\n"),
        ]
    )
    script = (
        f"const SF = {{}};\nconst STRINGS = {json.dumps(strings, ensure_ascii=False)};\n"
        f"{helpers}\nlet out;\n{body}\nprocess.stdout.write(JSON.stringify(out));\n"
    )
    result = subprocess.run(  # via stdin: the strings are too long for a command line
        [NODE, "-"], input=script, capture_output=True, timeout=20, encoding="utf-8"
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_t_fills_placeholders_and_keeps_unknown_ones():
    strings = {"hello": "Hello {name}, {n} new", "plain": "Plain"}
    out = run_js(
        strings,
        'out = [t("hello", {name: "Eva", n: 3}), t("hello", {name: "Eva"}), t("plain"), t("missing_key")];',
    )
    assert out == ["Hello Eva, 3 new", "Hello Eva, {n} new", "Plain", "missing_key"]


def test_js_plural_forms_match_the_server():
    strings = i18n.STRINGS["cs"]
    numbers = list(range(26)) + [101, 102, 105]
    out = run_js(
        strings, f"out = {json.dumps(numbers)}.map((n) => SF.plural(n, 'seasons'));"
    )
    from flask import Flask

    with Flask(__name__).test_request_context("/", headers={"Cookie": "lang=cs"}):
        expected = [i18n.plural(n, "seasons") for n in numbers]
    assert out == expected


@pytest.mark.parametrize("lang", ["en", "cs"])
def test_every_string_formats_the_same_in_the_browser(lang):
    strings = {**i18n.STRINGS["en"], **i18n.STRINGS[lang]}
    params = {
        key: {
            name: f"<{name}>"
            for _, name, _, _ in string.Formatter().parse(text)
            if name
        }
        for key, text in strings.items()
    }
    out = run_js(
        strings,
        f"const params = {json.dumps(params)};\n"
        "out = Object.fromEntries(Object.keys(params).map((k) => [k, t(k, params[k])]));",
    )
    expected = {key: text.format(**params[key]) for key, text in strings.items()}
    assert {k: v for k, v in out.items() if v != expected[k]} == {}


def test_is_finished_from_92_percent():
    out = run_js(
        {},
        "out = [SF.isFinished({position: 92, duration: 100}), SF.isFinished({position: 91.9, duration: 100}),"
        " SF.isFinished({position: 5}), SF.isFinished(null), SF.isFinished({position: 100, duration: 100})];",
    )
    assert out == [True, False, False, False, True]


def test_old_czech_urls_become_english():
    out = run_js(
        {},
        'out = [englishUrl("/film/5"), englishUrl("/serial/7?season=2"), englishUrl("/movie/1"),'
        ' englishUrl("/films/1"), englishUrl(undefined) ?? null];',
    )
    assert out == ["/movie/5", "/show/7?season=2", "/movie/1", "/films/1", None]
