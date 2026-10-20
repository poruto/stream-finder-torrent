import json
import random
from types import SimpleNamespace

import pytest

from streamfinder import config, create_app
from streamfinder import subtitle_api as api_module
from streamfinder.subtitles.sync import (
    Piece,
    align,
    confirm,
    fit_pairs,
    mapped,
    match_words,
    merge,
    spoken_words,
)


def _speech(seed=1, start=0.0, length=120.0):
    """Random speech-like on/off pattern."""
    rnd, t, out = random.Random(seed), start, []
    while t < start + length:
        talk = rnd.uniform(1.0, 4.0)
        out.append((t, min(t + talk, start + length)))
        t += talk + rnd.uniform(0.4, 2.5)
    return out


def test_finds_constant_offset():
    speech = _speech(start=600)
    cues = [(s - 3.2, e - 3.2) for s, e in speech]  # subtitles 3.2 s too early
    result = align(cues, speech, (600, 720))
    assert result.applied and abs(result.offset - 3.2) < 0.15 and result.scale == 1.0


def test_fixes_pal_frame_rate_drift():
    speech = _speech(seed=7, start=3000)
    pal = 23.976 / 25  # subtitles timed for a 25 fps release
    cues = [(s * pal, e * pal) for s, e in speech]
    result = align(cues, speech, (3000, 3120))
    assert result.applied and abs(result.scale - 25 / 23.976) < 1e-4
    centre = 3060
    assert abs(centre * pal * result.scale + result.offset - centre) < 0.2


def test_leaves_good_subtitles_alone():
    speech = _speech(seed=3)
    result = align(list(speech), speech, (0, 120))
    assert not result.applied and result.baseline > 0.95


# -- realistic data: a film's dialogue, its subtitles and what Whisper hears ----------

VOCABULARY = [
    "where",
    "are",
    "you",
    "going",
    "tonight",
    "I",
    "told",
    "have",
    "to",
    "work",
    "late",
    "always",
    "say",
    "that",
    "this",
    "time",
    "it",
    "is",
    "true",
    "promise",
    "then",
    "call",
    "me",
    "when",
    "get",
    "there",
    "will",
    "don't",
    "wait",
    "for",
    "did",
    "hear",
    "noise",
    "outside",
    "only",
    "the",
    "wind",
    "go",
    "back",
    "sleep",
    "morning",
    "coffee",
    "never",
    "again",
    "listen",
    "carefully",
    "nobody",
    "knows",
    "about",
    "money",
    "we",
    "need",
    "leave",
    "now",
    "open",
    "door",
    "slowly",
    "who",
    "sent",
    "here",
    "remember",
    "what",
    "happened",
    "last",
    "summer",
    "brother",
    "car",
    "keys",
    "station",
]


def _dialogue(seed, start, length):
    """Spoken lines: (start, end, words with their own start times), in video time."""
    rnd, t, lines = random.Random(seed), start, []
    while t < start + length:
        words, times = [], []
        for _ in range(rnd.randint(3, 8)):
            times.append(t)
            words.append(rnd.choice(VOCABULARY))
            t += rnd.uniform(0.2, 0.45)
        lines.append((times[0], t, list(zip(times, words, strict=True))))
        t += rnd.uniform(0.5, 3.0)
    return lines


def _subtitles(lines, to_file, seed=5):
    """Cues as a subtitle author times them, in the subtitle file's own clock."""
    rnd = random.Random(seed)
    cues = []
    for start, end, words in lines:
        s = to_file(start) + rnd.gauss(0, 0.04)  # a frame or so early or late
        e = to_file(end) + rnd.uniform(0.3, 0.9)  # stays up to be read
        cues.append((s, e, " ".join(w for _, w in words)))
    return cues


def _heard(lines, seed=9):
    """What Whisper makes of it: word times a little off, some words and lines wrong."""
    rnd = random.Random(seed)
    words = []
    for _, _, spoken in lines:
        if rnd.random() < 0.1:
            continue  # line not heard (music over it)
        for t, w in spoken:
            if rnd.random() < 0.1:
                w = rnd.choice(VOCABULARY)  # misheard
            words.append((t + rnd.gauss(0, 0.03), w, True))
    return words


def _worst(pieces, cues, truth, window):
    lo, hi = window
    errors = [
        abs(mapped(pieces, s) - truth(s)) for s, _, _ in cues if lo <= truth(s) <= hi
    ]
    assert errors
    return max(errors)


def test_words_find_offset_to_a_few_hundredths():
    lines = _dialogue(1, 1200, 90)
    cues = _subtitles(lines, lambda t: t - 4.37)  # subtitles 4.37 s early
    result = fit_pairs(match_words(cues, _heard(lines)))
    assert result.applied and result.details["sure"] and len(result.pieces) == 1
    assert result.pieces[0].scale == 1.0
    assert abs(result.pieces[0].offset - 4.37) < 0.05
    assert _worst(result.pieces, cues, lambda s: s + 4.37, (1200, 1290)) < 0.1


def test_words_find_frame_rate_drift():
    pal = 25 / 23.976  # subtitles of a 25 fps release on a 23.976 fps video
    lines = _dialogue(2, 3000, 90)
    cues = _subtitles(lines, lambda t: (t - 1.5) / pal)
    result = fit_pairs(match_words(cues, _heard(lines, seed=4)))
    assert result.applied and abs(result.scale - pal) < 1e-6
    assert _worst(result.pieces, cues, lambda s: s * pal + 1.5, (3000, 3090)) < 0.1


def test_words_find_a_cut_inside_the_stretch():
    # The video lacks 6 s the subtitles have (a recap, an ad break): two offsets.
    lines = _dialogue(3, 600, 120)

    def to_file(t):
        return t - 2.0 if t < 660 else t + 4.0

    def truth(s):
        return s + 2.0 if s < 658 else s - 4.0

    cues = _subtitles(lines, to_file)
    result = fit_pairs(match_words(cues, _heard(lines, seed=6)))
    assert result.applied and len(result.pieces) == 2
    early, late = result.pieces
    assert abs(early.offset - 2.0) < 0.05 and abs(late.offset + 4.0) < 0.05
    assert _worst(result.pieces, cues, truth, (600, 658)) < 0.1
    assert _worst(result.pieces, cues, truth, (661, 720)) < 0.1


def test_words_leave_synced_subtitles_alone():
    lines = _dialogue(4, 300, 90)
    cues = _subtitles(lines, lambda t: t + 0.06)
    result = fit_pairs(match_words(cues, _heard(lines)))
    assert result.details["sure"] and not result.applied
    assert result.pieces == [
        Piece(result.pieces[0].start, result.pieces[0].end, 1.0, 0.0)
    ]


def test_words_of_another_film_change_nothing():
    cues = _subtitles(_dialogue(5, 900, 90), lambda t: t - 3)
    result = fit_pairs(match_words(cues, _heard(_dialogue(6, 900, 90))))
    assert not result.applied and not result.pieces


def test_one_good_place_is_not_enough():
    # Five matching lines within ten seconds: could be a repeated scene - not certain.
    lines = _dialogue(7, 500, 10)
    cues = _subtitles(lines, lambda t: t - 3)
    result = fit_pairs(match_words(cues, _heard(lines)))
    assert not result.details.get("sure") and not result.pieces


@pytest.mark.parametrize("shift", [-6.83, 2.41])
def test_speech_lines_snap_to_where_speech_starts(shift):
    # Detected speech: the dialogue plus noise (door slams), lines stay up longer
    # than the voice, some lines have no detectable speech at all.
    rnd = random.Random(11)
    lines = _dialogue(8, 2400, 120)
    speech = [(s, e) for s, e, _ in lines if rnd.random() > 0.1]
    speech += [(2400 + rnd.uniform(0, 120), 0) for _ in range(6)]
    speech = sorted((s, e or s + rnd.uniform(0.3, 0.8)) for s, e in speech)
    cues = [(s, e) for s, e, _ in _subtitles(lines, lambda t: t - shift)]
    result = align(cues, speech, (2400, 2520))
    assert result.applied and abs(result.offset - shift) < 0.1


def test_speech_confirmation_covers_both_stretches():
    lines = _dialogue(9, 0, 400)
    speech = [(s, e) for s, e, _ in lines]
    cues = [(s, e) for s, e, _ in _subtitles(lines, lambda t: t - 3.0)]
    first, second = align(cues, speech, (250, 370)), align(cues, speech, (100, 220))
    both = confirm(first, second)
    assert both.applied and len(both.pieces) == 1
    piece = both.pieces[0]
    assert piece.start < 100 and piece.end > 360 and abs(piece.offset - 3.0) < 0.1


# -- the mapping over the whole film ------------------------------------------------------


def test_mapping_joins_pieces_with_straight_lines():
    pieces = [Piece(100, 200, 1.0, 2.0), Piece(1000, 1100, 1.0, 5.0)]
    assert mapped(pieces, 150) == 152  # inside
    assert mapped(pieces, 0) == 2  # before the first
    assert mapped(pieces, 2000) == 2005  # after the last
    assert mapped(pieces, 600) == pytest.approx(600 + 3.5)  # half way between
    assert mapped([], 42) == 42


def test_merge_keeps_cuts_and_joins_what_agrees():
    pieces = merge([], Piece(100, 200, 1.0, 2.0))
    pieces = merge(pieces, Piece(1000, 1100, 1.0, 5.0))  # after a cut
    assert len(pieces) == 2
    pieces = merge(pieces, Piece(1500, 1600, 1.0, 5.1))  # same offset again
    assert len(pieces) == 2 and pieces[1] == Piece(1000, 1600, 1.0, 5.1)
    pieces = merge(
        pieces, Piece(1050, 1150, 1.0, 9.0)
    )  # a fresher, different measurement
    assert [p.offset for p in pieces] == [2.0, 9.0]


# -- what Whisper sends -------------------------------------------------------------------


def test_spoken_words_from_whisper_cpp_tokens():
    data = {
        "segments": [
            {
                "start": 0.0,
                "words": [
                    {"word": "[_BEG_]", "t_dtw": -1},
                    {"word": " Don", "t_dtw": 120},
                    {"word": "'t", "t_dtw": 135},
                    {"word": " go", "t_dtw": 160},
                    {"word": "!", "t_dtw": 170},
                ],
            }
        ]
    }
    assert spoken_words(data, 100.0) == [
        (101.2, "don", True),
        (101.2, "t", False),
        (101.6, "go", True),
    ]


def test_spoken_words_from_groq_and_plain_segments():
    groq = {"words": [{"word": "Hello", "start": 0.5}, {"word": "there", "start": 0.9}]}
    assert spoken_words(groq, 10) == [(10.5, "hello", True), (10.9, "there", True)]
    plain = {"segments": [{"start": 2.0, "text": " Hi you"}]}
    assert spoken_words(plain, 10) == [(12.25, "hi", True), (12.25, "you", False)]


# -- the endpoint's steps ------------------------------------------------------------------


class _FakeTranscriber:
    available = True

    def __init__(self, lines):
        self.lines, self.calls = lines, 0

    def extract_audio(self, url, start, duration, audio_track=0):
        self.calls += 1
        return f"{start}:{duration}".encode().ljust(4000)

    def transcribe(self, audio):
        start, duration = map(float, audio.decode().split(":"))
        words = [
            {"word": w, "start": t - start}
            for _, _, spoken in self.lines
            for t, w in spoken
            if start <= t < start + duration
        ]
        return {"words": words, "language": "english"}


class _FakeSubtitles:
    def __init__(self, cues):
        self._cues = cues

    def language_of(self, candidate):
        return "en"


class _FakeServices:
    def __init__(self, lines, cues):
        self.transcriber = _FakeTranscriber(lines)
        self.subtitles = _FakeSubtitles(cues)


def test_sync_steps_report_progress_and_remember_pieces(monkeypatch):
    monkeypatch.setattr(api_module, "_sync_maps", api_module.LRU(4))
    monkeypatch.setattr(api_module, "_heard", api_module.LRU(40))
    lines = _dialogue(10, 0, 1500)

    def to_file(t):
        return t - 1.0 if t < 1000 else t - 8.0  # 7 s extra in the video at 1000 s

    cues = _subtitles(lines, to_file)
    svc = _FakeServices(lines, cues)
    steps = list(api_module._sync_steps(svc, "u", "c", cues, 400, 0))
    assert steps[0] == {"stage": "listening", "step": 1, "steps": 5}
    assert {"stage": "matching"} in steps
    first = steps[-1]
    assert first["done"] and first["certain"] and first["changed"]
    assert abs(first["offset"] - 1.0) < 0.05 and len(first["pieces"]) == 1

    later = list(api_module._sync_steps(svc, "u", "c", cues, 1300, 0))[-1]
    assert later["certain"] and abs(later["offset"] - 8.0) < 0.05
    assert [round(p["offset"]) for p in later["pieces"]] == [1, 8]

    calls = svc.transcriber.calls
    again = list(api_module._sync_steps(svc, "u", "c", cues, 1300, 0))[-1]
    assert again["certain"] and not again["changed"]
    assert svc.transcriber.calls == calls  # the transcripts were kept


def test_sync_endpoint_streams_progress_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_FILE", str(tmp_path / "settings.env"))
    monkeypatch.setattr(api_module, "_sync_maps", api_module.LRU(4))
    monkeypatch.setattr(api_module, "_heard", api_module.LRU(40))
    lines = _dialogue(12, 0, 300)
    cues = _subtitles(lines, lambda t: t - 2.5)
    svc = _FakeServices(lines, cues)
    svc.subtitles.cues = lambda candidate: cues
    svc.transcoder = SimpleNamespace(available=True)
    svc.torrserver = SimpleNamespace(
        file=lambda *a, **k: SimpleNamespace(is_video=True),
        stream_url=lambda info_hash, file: "u",
    )
    app = create_app(testing=True)
    app.extensions["streamfinder"] = svc
    body = {"hash": "a" * 40, "file_index": 0, "subtitle": "c", "position": 150}
    response = app.test_client().post(
        "/api/subtitles/sync", json=body, headers={"Accept": "application/x-ndjson"}
    )
    assert response.mimetype == "application/x-ndjson"
    steps = [json.loads(line) for line in response.get_data(as_text=True).splitlines()]
    assert steps[0]["stage"] == "listening" and steps[-1]["done"]
    assert steps[-1]["certain"] and abs(steps[-1]["offset"] - 2.5) < 0.05
    # Without the Accept header: just the result, as plain JSON.
    plain = app.test_client().post("/api/subtitles/sync", json=body).get_json()
    assert plain["success"] and plain["certain"] and not plain["changed"]
