"""Subtitle finder, cache, machine translation and AI-subtitle helpers (no network)."""

import io
import zipfile

import pytest
import requests

from streamfinder import config
from streamfinder.matching import parse_release
from streamfinder.subtitles import service as service_module
from streamfinder.subtitles.opensubtitles_org import OpenSubtitlesOrgClient, OrgSubtitle
from streamfinder.subtitles.service import (
    SubtitleCandidate,
    SubtitleService,
    _subtitle_belongs_to,
)
from streamfinder.subtitles.transcribe import (
    LEAD,
    Line,
    TranscribeError,
    Transcriber,
    _language_code,
    _word_times,
    clean_segments,
    merge_sentences,
    pick_speech_model,
)
from streamfinder.subtitles.translate import TranslateError, Translator, _batches
from streamfinder.torrserver import TorrentFile

SRT = "1\n00:00:01,000 --> 00:00:02,000\nAhoj\n\n2\n00:00:03,000 --> 00:00:04,000\nSvěte & spol.\n"
HASH = "a" * 40


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise requests.ConnectionError("network disabled in tests")

    monkeypatch.setattr(requests.Session, "send", refuse)


class FakeTorrServer:
    def __init__(self, files, contents=None):
        self._files, self.contents = files, contents or {}

    def files(self, info_hash, wait=0):
        return self._files

    def file(self, info_hash, index, wait=0):
        return next((f for f in self._files if f.index == index), None)

    def read_file(self, info_hash, file):
        return self.contents[file.index]

    def read_range(self, info_hash, file, start, length, timeout=30):
        raise requests.ConnectionError("no data")


@pytest.fixture
def make_service(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OPENSUBTITLES_API_KEY", "")
    monkeypatch.setattr(config, "OPENSUBTITLES_USERNAME", "")
    monkeypatch.setattr(config, "OPENSUBTITLES_PASSWORD", "")

    def make(files=(), contents=None, languages=("cs", "en")):
        monkeypatch.setattr(config, "SUBTITLE_LANGUAGES", list(languages))
        svc = SubtitleService(
            FakeTorrServer(list(files), contents), cache_dir=str(tmp_path)
        )
        monkeypatch.setattr(svc.org, "search", lambda imdb, langs: [])
        return svc

    return make


# -- which bundled subtitle belongs to which video -----------------------------------------


def test_single_video_takes_every_subtitle():
    video = TorrentFile(1, "Movie/Movie.mkv", 10)
    assert _subtitle_belongs_to(TorrentFile(2, "Movie/Subs/whatever.srt", 1), video, 1)


def test_pack_subtitle_matches_by_file_stem_or_episode():
    video = TorrentFile(2, "Show.S01/Show.S01E02.1080p.mkv", 10)
    by_stem = TorrentFile(5, "Show.S01/Subs/Show.S01E02.1080p/2_English.srt", 1)
    by_episode = TorrentFile(6, "Show.S01/Subs/S01E02.cs.srt", 1)
    other = TorrentFile(7, "Show.S01/Subs/Show.S01E03.cs.srt", 1)
    wrong_season = TorrentFile(8, "Show.S01/Subs/S02E02.cs.srt", 1)
    assert _subtitle_belongs_to(by_stem, video, 10)
    assert _subtitle_belongs_to(by_episode, video, 10)
    assert not _subtitle_belongs_to(other, video, 10)
    assert not _subtitle_belongs_to(wrong_season, video, 10)


def test_bundled_subtitles_language_and_flags(make_service):
    files = [
        TorrentFile(0, "Movie/Movie.mkv", 10**9),
        TorrentFile(1, "Movie/Subs/Movie.cze.srt", 10**4),
        TorrentFile(2, "Movie/Subs/Movie.eng.SDH.srt", 10**4),
        TorrentFile(3, "Movie/Subs/Movie.eng.forced.srt", 10**3),
        TorrentFile(4, "Movie/Subs/Movie.ger.srt", 10**4),  # not wanted
        TorrentFile(5, "Movie/Subs/Movie.srt", 10**4),  # no language: assumed English
    ]
    svc = make_service(files)
    found = {c.id: c for c in svc._bundled(HASH, files, files[0], ["cs", "en"])}
    assert set(found) == {f"tor-{HASH}-{i}" for i in (1, 2, 3, 5)}
    assert found[f"tor-{HASH}-1"].language == "cs"
    assert found[f"tor-{HASH}-2"].hearing_impaired
    assert (
        found[f"tor-{HASH}-3"].score < found[f"tor-{HASH}-2"].score
    )  # forced: few lines
    assert found[f"tor-{HASH}-5"].language == "en"


def test_vobsub_images_are_not_text_subtitles(make_service):
    files = [
        TorrentFile(0, "Movie/Movie.mkv", 10**9),
        TorrentFile(1, "Movie/Movie.idx", 10**4),
        TorrentFile(2, "Movie/Movie.sub", 10**6),
    ]
    assert make_service(files)._bundled(HASH, files, files[0], ["en"]) == []


def test_find_ranks_bundled_first_and_picks_best_per_language(make_service):
    files = [
        TorrentFile(0, "Show.S01/Show.S01E01.mkv", 10**9),
        TorrentFile(1, "Show.S01/Show.S01E02.mkv", 10**9),
        TorrentFile(2, "Show.S01/Show.S01E02.en.srt", 10**4),
        TorrentFile(3, "Show.S01/Show.S01E01.cs.srt", 10**4),
    ]
    svc = make_service(files)
    result = svc.find(HASH, file_index=1, media_type="tv", languages=["cs", "en"])
    assert result["video"]["index"] == 1
    assert [c["id"] for c in result["candidates"]] == [f"tor-{HASH}-2"]
    assert result["best"]["language"] == "en" and result["best"]["note"] == "torrent"
    assert result["warnings"] == []


def test_find_without_any_subtitle_warns(make_service):
    files = [TorrentFile(0, "Movie.2019.1080p.mkv", 10**9)]
    result = make_service(files).find(HASH)
    assert result["candidates"] == [] and result["best"] is None
    assert result["warnings"] == ["no_online"]


def test_find_without_a_video_is_an_error(make_service):
    from streamfinder.torrserver import TorrServerError

    with pytest.raises(TorrServerError) as error:
        make_service([TorrentFile(0, "readme.txt", 10)]).find(HASH)
    assert error.value.code == "no_video"


def test_failing_opensubtitles_org_is_only_a_warning(make_service, monkeypatch):
    files = [TorrentFile(0, "Movie.2019.1080p.mkv", 10**9)]
    svc = make_service(files)

    def broken(imdb, languages):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(svc.org, "search", broken)
    result = svc.find(HASH, imdb_id="tt0111161")
    assert result["warnings"] == ["no_online"]


def test_online_results_are_merged_and_prefer_org_downloads(make_service):
    video = parse_release("Movie.2019.1080p.WEB-DL.x264-GRP.mkv")
    api = [
        {
            "attributes": {
                "language": "cs",
                "release": "Movie.2019.1080p.WEB-DL.x264-GRP",
                "legacy_subtitle_id": 77,
                "download_count": 1000,
                "moviehash_match": True,
                "files": [{"file_id": 1}],
            }
        },
        {
            "attributes": {"language": "pt-BR", "files": [{"file_id": 2}]}
        },  # unwanted language
        {"attributes": {"language": "en", "files": []}},  # nothing to download
        {
            "attributes": {
                "language": "en",
                "release": "Movie.2019.720p.HDTV",
                "machine_translated": True,
                "hearing_impaired": True,
                "files": [{"file_id": 3}, {"file_id": 4}],
            }
        },
    ]
    org = [
        OrgSubtitle(77, "cs", "dup of the api one", 5, None, 1, "srt"),
        OrgSubtitle(88, "en", "Movie.2019.1080p.WEB-DL.x264-GRP", 50, 23.976, 2, "srt"),
        OrgSubtitle(99, "de", "German", 5, None, 1, "srt"),
    ]
    merged = {
        c.id: c for c in SubtitleService._merge_online(api, org, video, ["cs", "en"])
    }
    assert set(merged) == {"org-77", "os-3", "org-88"}
    assert merged["org-77"].hash_match and merged["org-77"].file_id == 1
    assert merged["org-77"].note == "hash" and merged["org-77"].score > 100
    assert (
        merged["os-3"].note == "machine"
        and merged["os-3"].score < merged["org-88"].score
    )
    assert merged["org-88"].fps == 23.976


def test_candidate_dict_for_the_player():
    candidate = SubtitleCandidate(
        "os-1", "cs", "opensubtitles", "rel", hearing_impaired=True
    )
    data = candidate.to_dict()
    assert data["url"] == "/api/subtitles/os-1.vtt"
    assert data["language_name"] == "Čeština" and data["note"] == "hi"
    assert SubtitleCandidate("x", "xx", "opensubtitles", "").language_name == "xx"


# -- downloads and the cache -------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", ["", "os-", "os-12a", "../../etc/passwd", "tor-zz-1", "org-1; rm"]
)
def test_invalid_subtitle_ids_are_refused(make_service, bad):
    with pytest.raises(ValueError):
        make_service().get_vtt(bad)


def test_bundled_subtitle_is_decoded_converted_and_cached(make_service, tmp_path):
    files = [TorrentFile(0, "Movie.mkv", 10**9), TorrentFile(1, "Movie.cze.srt", 100)]
    svc = make_service(files, {1: SRT.encode("cp1250")})
    vtt = svc.get_vtt(f"tor-{HASH}-1")
    assert vtt.startswith("WEBVTT") and "Světe &amp; spol." in vtt
    svc.torrserver.contents.clear()  # a second read must come from the cache
    assert svc.get_vtt(f"tor-{HASH}-1") == vtt
    assert [p.suffix for p in tmp_path.iterdir()] == [".vtt"]
    assert svc.cues(f"tor-{HASH}-1") == [
        (1.0, 2.0, "Ahoj"),
        (3.0, 4.0, "Světe &amp; spol."),
    ]


def test_bundled_id_of_a_non_subtitle_file_is_refused(make_service):
    files = [TorrentFile(0, "Movie.mkv", 10**9)]
    with pytest.raises(ValueError):
        make_service(files).get_vtt(f"tor-{HASH}-0")


def test_unconvertible_subtitle_is_an_error_and_not_cached(make_service, tmp_path):
    files = [TorrentFile(1, "Movie.srt", 100)]
    svc = make_service(files, {1: b"\x00\x01 binary junk"})
    with pytest.raises(ValueError):
        svc.get_vtt(f"tor-{HASH}-1")
    assert list(tmp_path.iterdir()) == []


def test_os_ids_need_the_api_key(make_service):
    with pytest.raises(RuntimeError):
        make_service().get_vtt("os-123")


def _zip(name, data):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("readme.nfo", "x" * 5000)
        archive.writestr(name, data)
    return buffer.getvalue()


def test_org_download_unpacks_the_subtitle(monkeypatch):
    client = OpenSubtitlesOrgClient()

    class Answer:
        content = _zip("Movie.cze.srt", SRT.encode("utf-8"))

        def raise_for_status(self):
            pass

    monkeypatch.setattr(client.session, "get", lambda *a, **k: Answer())
    assert client.download(5) == SRT.encode("utf-8")
    Answer.content = b"<html>captcha</html>"
    with pytest.raises(RuntimeError, match="captcha"):
        client.download(5)


def test_org_bot_check_switches_to_the_api_for_an_hour(make_service, monkeypatch):
    svc = make_service()
    svc.api.api_key = "key"
    calls = []

    def blocked(id_subtitle):
        calls.append("org")
        raise RuntimeError("OpenSubtitles.org odmítl stažení (limit nebo captcha)")

    monkeypatch.setattr(svc.org, "download", blocked)
    monkeypatch.setattr(
        svc.api,
        "download",
        lambda file_id: calls.append(("api", file_id)) or SRT.encode(),
    )
    svc._meta["org-5"] = SubtitleCandidate(
        "org-5", "cs", "opensubtitles", "", file_id=42
    )
    assert "Ahoj" in svc.get_vtt("org-5")
    assert svc.org_blocked
    svc._meta["org-6"] = SubtitleCandidate(
        "org-6", "cs", "opensubtitles", "", file_id=43
    )
    svc.get_vtt("org-6")
    assert calls == ["org", ("api", 42), ("api", 43)]  # .org not asked again meanwhile
    # Without an API fallback the subtitle can't be had while .org is blocked.
    with pytest.raises(RuntimeError):
        svc.get_vtt("org-7")


def test_org_failure_without_fallback_is_raised(make_service, monkeypatch):
    svc = make_service()
    monkeypatch.setattr(
        svc.org, "download", lambda i: (_ for _ in ()).throw(RuntimeError("limit"))
    )
    with pytest.raises(RuntimeError, match="limit"):
        svc.get_vtt("org-5")
    assert not svc.org_blocked


def test_blocked_org_hides_candidates_the_api_cannot_deliver(make_service, monkeypatch):
    files = [TorrentFile(0, "Movie.2019.1080p.mkv", 10**9)]
    svc = make_service(files)
    monkeypatch.setattr(
        svc.org,
        "search",
        lambda imdb, langs: [OrgSubtitle(9, "cs", "x", 1, None, 1, "srt")],
    )
    assert [c["id"] for c in svc.find(HASH, imdb_id="tt1")["candidates"]] == ["org-9"]
    svc._org_blocked_until = float("inf")
    assert svc.find(HASH, imdb_id="tt1")["candidates"] == []


def test_translated_subtitle_is_cached_per_language_and_engine(make_service):
    files = [TorrentFile(1, "Movie.srt", 100)]
    svc = make_service(files, {1: SRT.encode()})

    class Translator:
        calls = []

        def translate(self, texts, target, engine):
            self.calls.append((target, engine))
            return [f"{target}:{t}" for t in texts]

    translator = Translator()
    cs = svc.translated_vtt(f"tor-{HASH}-1", "de", "google", translator)
    assert "00:00:01.000 --> 00:00:02.000\nde:Ahoj" in cs
    assert svc.translated_vtt(f"tor-{HASH}-1", "de", "google", translator) == cs
    svc.translated_vtt(f"tor-{HASH}-1", "de", "ai", translator)
    svc.translated_vtt(f"tor-{HASH}-1", "fr", "google", translator)
    assert translator.calls == [("de", "google"), ("de", "ai"), ("fr", "google")]


def test_failed_translation_leaves_no_cache(make_service, tmp_path):
    files = [TorrentFile(1, "Movie.srt", 100)]
    svc = make_service(files, {1: SRT.encode()})

    class Broken:
        def translate(self, texts, target, engine):
            raise TranslateError("429")

    with pytest.raises(TranslateError):
        svc.translated_vtt(f"tor-{HASH}-1", "de", "google", Broken())
    assert len(list(tmp_path.iterdir())) == 1  # only the original subtitle


def test_language_of_a_known_candidate(make_service):
    svc = make_service()
    svc._meta["os-1"] = SubtitleCandidate("os-1", "sk", "opensubtitles", "")
    assert svc.language_of("os-1") == "sk" and svc.language_of("os-2") is None


def test_status_lists_language_names(make_service):
    status = make_service(languages=("cs", "xx")).status()
    assert status["language_names"] == {"cs": "Čeština", "xx": "xx"}
    assert status["opensubtitles_api"] is False


def test_moviehash_failure_is_none_not_an_error(make_service):
    svc = make_service()
    assert svc._moviehash(HASH, TorrentFile(0, "Movie.mkv", 10**9)) is None


# -- machine translation -------------------------------------------------------------------


def test_batches_respect_the_character_limit():
    lines = ["a" * 10] * 10
    batches = _batches(lines, 35)
    assert [len(b) for b in batches] == [3, 3, 3, 1]
    assert [line for batch in batches for line in batch] == lines
    assert _batches([], 10) == []
    # A single line longer than the limit still gets its own batch.
    assert _batches(["x" * 50, "y"], 10) == [["x" * 50], ["y"]]


class GoogleAnswer:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return [[[part, None] for part in self.text.splitlines(keepends=True)]]


def test_google_translation_one_line_per_line(monkeypatch):
    translator = Translator()
    sent = []

    def post(url, params, data, timeout):
        sent.append(data["q"])
        return GoogleAnswer(data["q"].upper())

    monkeypatch.setattr(translator.session, "post", post)
    assert translator.translate(["hello\nthere", "  bye  "], "cs") == [
        "HELLO THERE",
        "BYE",
    ]
    assert sent == ["hello there\nbye"]  # line breaks inside a line become spaces
    assert translator.translate([], "cs") == []


def test_google_merging_lines_falls_back_to_one_by_one(monkeypatch):
    translator = Translator()

    def post(url, params, data, timeout):
        text = data["q"]
        return GoogleAnswer(text.replace("\n", " ") if "\n" in text else f"[{text}]")

    monkeypatch.setattr(translator.session, "post", post)
    assert translator.translate(["one", "two", "three"], "cs") == [
        "[one]",
        "[two]",
        "[three]",
    ]


def test_google_empty_answer_keeps_the_original(monkeypatch):
    translator = Translator()
    monkeypatch.setattr(translator.session, "post", lambda *a, **k: GoogleAnswer("\n"))
    assert translator.translate(["one", "two"], "cs") == ["one", "two"]


def test_google_failure_is_a_translate_error():
    with pytest.raises(TranslateError, match="Google Translate"):
        Translator().translate(["hello"], "cs")


def test_large_subtitles_are_translated_in_batches(monkeypatch):
    translator = Translator()
    calls = []

    def post(url, params, data, timeout):
        calls.append(data["q"].count("\n") + 1)
        return GoogleAnswer(data["q"])

    monkeypatch.setattr(translator.session, "post", post)
    lines = [f"line number {i} " + "x" * 80 for i in range(200)]
    assert translator.translate(lines, "cs") == lines
    assert len(calls) > 1 and sum(calls) == 200


def test_ai_engine_needs_a_chat_model_and_batches_by_lines():
    assert Translator().engines() == ["google"]
    with pytest.raises(TranslateError):
        Translator().translate(["x"], "cs", engine="ai")

    class Chat:
        chat_available = True
        sizes = []

        def translate(self, batch, target):
            self.sizes.append(len(batch))
            return [f"{target}:{t}" for t in batch]

    chat = Chat()
    translator = Translator(chat)
    assert translator.engines() == ["google", "ai"]
    out = translator.translate([str(i) for i in range(95)], "cs", engine="ai")
    assert out[0] == "cs:0" and out[-1] == "cs:94" and len(out) == 95
    assert sorted(chat.sizes) == [15, 40, 40]


def test_ai_engine_errors_become_translate_errors():
    class Chat:
        chat_available = True

        def translate(self, batch, target):
            raise TranscribeError("translation returned a different number of lines")

    with pytest.raises(TranslateError, match="different number"):
        Translator(Chat()).translate(["a"], "cs", engine="ai")


# -- AI subtitles: whisper output -> lines ----------------------------------------------------------


def seg(start, end, text, **extra):
    return {"start": start, "end": end, "text": text, **extra}


@pytest.mark.parametrize(
    "text", ["[MUSIC PLAYING]", "(laughs)", "♪ la la ♪", "*sigh*", "...", "-", "   "]
)
def test_sound_tags_and_empty_lines_are_not_speech(text):
    assert clean_segments([seg(0, 2, text)], 0, 30) == []


def test_speech_next_to_a_sound_tag_is_kept():
    lines = clean_segments([seg(0, 2, "[door slams] Who is there?")], 0, 30)
    assert [line.text for line in lines] == ["[door slams] Who is there?"]


@pytest.mark.xfail(
    reason="BUG: the sound-tag filter (_SOUND_TAG) matches any text that starts and "
    "ends with a bracket, so real speech between two tags is dropped",
    strict=True,
)
def test_speech_between_two_sound_tags_is_kept():
    lines = clean_segments([seg(0, 2, "(sighs) Fine, let's go. (laughs)")], 0, 30)
    assert lines and "Fine, let's go." in lines[0].text


def test_unsure_but_plausible_speech_is_kept():
    muffled = seg(1, 3, "Get down!", no_speech_prob=0.7, avg_logprob=-0.5)
    assert [line.text for line in clean_segments([muffled], 0, 30)] == ["Get down!"]


def test_speaker_dash_is_removed_and_spaces_collapsed():
    lines = clean_segments([seg(1, 3, "-  Where   are you?")], 0, 30)
    assert lines[0].text == "Where are you?"


def test_repeated_line_becomes_one_longer_cue():
    lines = clean_segments([seg(1, 2, "No."), seg(2.2, 3, "No.")], 0, 30)
    assert len(lines) == 1 and lines[0].start == 1 + LEAD


def test_line_times_are_capped_by_reading_speed_and_the_next_line():
    lines = clean_segments([seg(0, 20, "Hi."), seg(1.5, 3, "Next one.")], 100, 130)
    first, second = lines
    assert (
        first.end <= second.start and first.end - first.start <= 1.2 + 0.07 * 3 + 0.01
    )
    assert second.start == 101.5 + LEAD


def test_line_ends_at_the_window_end():
    lines = clean_segments([seg(28, 40, "Cut at the edge of the window.")], 0, 30)
    assert lines[0].end <= 30


def test_whisper_cpp_word_times_set_the_line_start():
    words = [
        {"word": " Hello", "t_dtw": 150},
        {"word": " there", "t_dtw": 190},
        {"word": "", "t_dtw": 300},
    ]
    assert _word_times({"words": words}, []) == (1.5, 1.9 + 0.5)
    lines = clean_segments([seg(1, 3, "Hello there", words=words)], 10, 40)
    assert lines[0].start == 11.5


def test_api_word_list_sets_the_line_start():
    words = [
        {"word": "Hello", "start": 1.3, "end": 1.6},
        {"word": "there", "start": 1.7, "end": 2.2},
        {"word": "later", "start": 9.0, "end": 9.4},
    ]
    assert _word_times(seg(1, 3, "Hello there"), words) == (1.3, 2.2)
    assert _word_times(seg(20, 22, "Nothing"), words) is None


def test_merge_keeps_sentence_ends_and_long_gaps_apart():
    lines = [
        Line(0, 1, "Hello."),
        Line(1.1, 2, "and"),  # previous line ended a sentence
        Line(5, 6, "after a long pause"),  # 3 s gap
        Line(6.1, 7, "x" * 120),  # too long to join
    ]
    assert [m.text for m in merge_sentences(lines)] == [line.text for line in lines]
    assert merge_sentences([]) == []


def test_merge_does_not_change_its_input():
    lines = [Line(0, 1, "I was"), Line(1.2, 2, "there.")]
    merged = merge_sentences(lines)
    assert merged[0].text == "I was there." and lines[0].text == "I was"


@pytest.mark.parametrize(
    "value, code",
    [
        ("english", "en"),
        ("Czech", "cs"),
        ("cs", "cs"),
        (" Japanese ", "ja"),
        ("Čeština", "cs"),
        ("klingon", "klingon"),
        ("", ""),
    ],
)
def test_language_codes_from_whisper(value, code):
    assert _language_code(value) == code


def test_speech_model_choice():
    assert pick_speech_model(["whisper-1", "whisper-large-v3"]) == "whisper-large-v3"
    assert pick_speech_model(["gpt-4o", "tts-1"]) is None
    assert pick_speech_model([]) is None


class _FakeTranscoder:
    available = True
    ffmpeg = "ffmpeg"


class ChatAnswer:
    def __init__(self, status=200, data=None, headers=None, text=""):
        self.status_code, self.data, self.headers, self.text = (
            status,
            data,
            headers or {},
            text,
        )
        self.ok = status < 400

    def json(self):
        if self.data is None:
            raise ValueError("no json")
        return self.data


def test_ai_translation_must_keep_the_line_count(monkeypatch):
    t = Transcriber(_FakeTranscoder(), "gsk_key")
    monkeypatch.setattr(t, "chat_model", lambda: "llama-3.3-70b-versatile")
    content = {"lines": ["Ahoj", "  Jak   se máš? ", ""]}
    import json

    answer = ChatAnswer(
        data={"choices": [{"message": {"content": json.dumps(content)}}]}
    )
    monkeypatch.setattr(t.session, "post", lambda *a, **k: answer)
    assert t.translate(["Hi", "How are you?", "Bye"], "cs") == [
        "Ahoj",
        "Jak se máš?",
        "Bye",
    ]
    with pytest.raises(TranscribeError, match="different number"):
        t.translate(["Hi"], "cs")
    answer.data = {"choices": [{"message": {"content": "not json"}}]}
    with pytest.raises(TranscribeError, match="no lines"):
        t.translate(["Hi"], "cs")


def test_ai_translation_without_chat_model_is_501(monkeypatch):
    t = Transcriber(_FakeTranscoder(), "gsk_key")
    monkeypatch.setattr(t, "chat_model", lambda: None)
    with pytest.raises(TranscribeError) as error:
        t.translate(["Hi"], "cs")
    assert error.value.status == 501


@pytest.mark.parametrize(
    "answer, status, retry",
    [
        (ChatAnswer(429, {}, {"retry-after": "7"}), 429, 7.0),
        (ChatAnswer(429, {}), 429, 20),
        (ChatAnswer(401, {}), 401, None),
        (ChatAnswer(500, {"error": {"message": "boom"}}), 502, None),
        (ChatAnswer(503, None, text="<html>down</html>"), 502, None),
    ],
)
def test_api_errors_become_transcribe_errors(answer, status, retry):
    with pytest.raises(TranscribeError) as error:
        Transcriber._raise_for(answer)
    assert error.value.status == status and error.value.retry_after == retry


@pytest.mark.xfail(
    reason="BUG: Transcriber._raise_for assumes the error body is "
    "{'error': {'message': ...}}; servers answering {'error': 'text'} (whisper.cpp, "
    "proxies) crash with AttributeError instead of a TranscribeError",
    strict=True,
)
def test_plain_string_error_bodies_are_handled():
    with pytest.raises(TranscribeError):
        Transcriber._raise_for(ChatAnswer(500, {"error": "model not loaded"}))


def test_chat_model_lookup_failure_is_retried_later(monkeypatch):
    t = Transcriber(_FakeTranscoder(), "gsk_key")
    calls = []

    def list_models(key=None, url=None):
        calls.append(url)
        raise requests.ConnectionError("down")

    monkeypatch.setattr(t, "list_models", list_models)
    assert t.chat_model() is None and t.chat_model() is None
    assert len(calls) == 1  # not again right away
    t._chat_retry_at = 0
    monkeypatch.setattr(
        t,
        "list_models",
        lambda key=None, url=None: ["llama-3.1-8b-instant", "gpt-4o-mini"],
    )
    assert t.chat_model() == "gpt-4o-mini"  # the better of the offered models


def test_provider_is_guessed_from_what_is_configured():
    assert Transcriber(_FakeTranscoder(), "", "").provider == ""
    assert not Transcriber(_FakeTranscoder(), "", "").available
    keyed = Transcriber(_FakeTranscoder(), "sk-openai")
    assert keyed.provider == "api" and keyed.api_url == "https://api.openai.com/v1"
    local = Transcriber(_FakeTranscoder(), "gsk_x", "http://pc:8178/v1/")
    assert local.provider == "local" and local.speech_key == "" and local.chat_available
    off = Transcriber(_FakeTranscoder(), "gsk_x", enabled=False)
    assert not off.available


def test_short_audio_window_is_empty_and_cached(monkeypatch):
    t = Transcriber(_FakeTranscoder(), "gsk_x")
    calls = []
    monkeypatch.setattr(t, "extract_audio", lambda *a: calls.append(a) or b"")
    first = t.window("http://x/s", 100.0, 30.0, language="en")
    assert first == {"lines": [], "language": "en", "translated": False}
    t.window("http://x/s", 100.0, 30.0, language="en")
    assert len(calls) == 1
    # The window is clamped to 5..60 s and cut out with overlap on both sides.
    t.window("http://x/s", 200.0, 600.0)
    assert calls[1][1:3] == (197.0, 60 + 3 + 3)


def test_window_translates_and_tags_the_language(monkeypatch):
    t = Transcriber(_FakeTranscoder(), "gsk_x")
    monkeypatch.setattr(t, "extract_audio", lambda *a: b"x" * 4000)
    asked = []

    def transcribe(audio, language="", task="transcribe"):
        asked.append(task)
        return {"segments": [seg(4.0, 6.0, "Hello there.")], "language": "english"}

    monkeypatch.setattr(t, "transcribe", transcribe)

    class Google:
        def translate(self, texts, target, engine):
            return [f"[{target}] {x}" for x in texts]

    t.translator = Google()
    result = t.window("http://x/s", 0.0, 30.0, translate_to="cs")
    assert result["translated"] and result["lines"][0]["text"] == "[cs] Hello there."
    # Whisper translated the speech itself: English text, Google did the rest.
    assert asked[0] == "translate" and result["via"] == "english"
    # No target language: plain transcription.
    plain = t.window("http://x/s", 30.0, 30.0)
    assert not plain["translated"] and plain["via"] == "direct"
    assert asked[-1] == "transcribe"


def test_failed_audio_cut_is_reported_for_a_retry(monkeypatch, tmp_path):
    import subprocess
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    t = Transcriber(_FakeTranscoder(), "gsk_x")

    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired("ffmpeg", 90)

    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(TranscribeError) as error:
        t.extract_audio("http://x/s", 0, 30)
    assert error.value.status == 504 and error.value.retry_after == 10
    # The temporary file and the extract slot are given back.
    assert t._extract_slots._value == 2
    assert list(tmp_path.iterdir()) == []


def test_service_module_ids_cover_every_candidate_kind():
    assert service_module._ID_RE.fullmatch("os-1")
    assert service_module._ID_RE.fullmatch("org-123")
    assert service_module._ID_RE.fullmatch(f"tor-{HASH}-12")
    assert not service_module._ID_RE.fullmatch(f"tor-{HASH.upper()}-12")
