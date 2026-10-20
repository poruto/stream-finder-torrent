import random
import re
import subprocess
import sys
import threading
import time
from difflib import SequenceMatcher

import pytest

from streamfinder import transcode
from streamfinder.caching import LRU, KeyedLocks
from streamfinder.matching import normalize_title, title_similarity
from streamfinder.subtitles.service import SubtitleService
from streamfinder.subtitles.sync import (
    FINE_STEP,
    MAX_SHIFT,
    SCALES,
    STEP,
    align,
    in_reach,
)
from streamfinder.subtitles.transcribe import TranscribeError, Transcriber
from streamfinder.transcode import Transcoder, TranscoderBusy

VTT = "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello\n"


def test_lru_forgets_least_recently_used():
    cache = LRU(2)
    cache["a"], cache["b"] = 1, 2
    assert cache.get("a") == 1  # "a" is now the most recent
    cache["c"] = 3
    assert "b" not in cache and cache.get("a") == 1 and cache.get("c") == 3


def test_keyed_locks_are_dropped_when_free():
    locks = KeyedLocks()
    with locks.hold("x"):
        assert "x" in locks._locks
    assert not locks._locks


# -- torrent title matching ----------------------------------------------------------


def _old_title_similarity(candidate, expected_titles):
    """The implementation before ratio() got its quick upper-bound gates."""
    candidate_norm = normalize_title(candidate)
    if not candidate_norm:
        return 0.0
    best = 0.0
    for expected in expected_titles:
        expected_norm = normalize_title(expected)
        if not expected_norm:
            continue
        variants = {
            expected_norm,
            re.sub(r"^(the|a|an) ", "", expected_norm),
            expected_norm.replace(" and ", " "),
        }
        cand_variants = {
            candidate_norm,
            re.sub(r"^(the|a|an) ", "", candidate_norm),
            candidate_norm.replace(" and ", " "),
        }
        for exp in variants:
            for cand in cand_variants:
                if exp == cand:
                    return 1.0
                ratio = SequenceMatcher(None, exp, cand).ratio()
                if cand.startswith(exp + " ") and len(cand) - len(exp) <= 4:
                    ratio = max(ratio, 0.9)
                best = max(best, ratio)
    return best


def test_title_similarity_matches_the_exact_computation():
    titles = ["The Matrix", "Matrix", "Pelíšky", "Fast & Furious", "", None]
    candidates = [
        "matrix resurrections",
        "the matrix",
        "matrix us",
        "pelisky",
        "fast and furious",
        "fast furious 7",
        "something else entirely",
        "",
    ]
    for candidate in candidates:
        assert title_similarity(candidate, titles) == _old_title_similarity(
            candidate, titles
        )


# -- subtitle sync -----------------------------------------------------------------


def test_dropped_cues_could_never_reach_the_window():
    window = (3000.0, 3120.0)
    cues = [(i * 1.5, i * 1.5 + 1) for i in range(5000)]  # a two-hour film
    kept = set(in_reach(cues, window))
    assert 0 < len(kept) < len(cues) / 10
    steps = int(MAX_SHIFT / STEP) + 6  # the coarse grid and the fine search around it
    offsets = [k * FINE_STEP for k in range(-steps * 5, steps * 5 + 1)]
    for s, e in cues:
        if (s, e) in kept:
            continue
        for scale in SCALES:
            for offset in offsets:
                assert not (
                    e * scale + offset > window[0] and s * scale + offset < window[1]
                )


def test_align_ignores_the_rest_of_the_film():
    rng = random.Random(3)
    speech, t = [], 100.0
    while t < 220:
        length = rng.uniform(0.8, 3.5)
        speech.append((t, t + length))
        t += length + rng.uniform(0.3, 2.5)
    near = [(s - 4.0, e - 4.0) for s, e in speech]
    far = [(400 + i * 3.0, 402 + i * 3.0) for i in range(2000)]
    full = align(near + far, speech, (100, 220))
    assert full == align(near, speech, (100, 220))
    assert full.applied and abs(full.offset - 4.0) < 0.1


# -- ffmpeg ----------------------------------------------------------------------------


def test_failed_probe_is_remembered_for_a_while(monkeypatch):
    t = Transcoder(ffmpeg="ffmpeg", ffprobe="ffprobe")
    calls = []

    def slow_failure(url, timeout):
        calls.append(url)
        time.sleep(0.2)
        raise subprocess.TimeoutExpired("ffprobe", timeout)

    monkeypatch.setattr(t, "_ffprobe", slow_failure)
    errors = []

    def probe():
        try:
            t.probe("http://x/stream")
        except RuntimeError as e:
            errors.append(e)

    threads = [threading.Thread(target=probe) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(calls) == 1 and len(errors) == 3  # one ffprobe for all three requests
    with pytest.raises(RuntimeError):
        t.probe("http://x/stream")
    assert len(calls) == 1
    later = time.monotonic() + transcode.PROBE_RETRY + 1
    monkeypatch.setattr(transcode.time, "monotonic", lambda: later)
    with pytest.raises(RuntimeError):
        t.probe("http://x/stream")
    assert len(calls) == 2  # tried again after PROBE_RETRY


FAKE_FFMPEG = "import sys, time; sys.stdout.buffer.write(b'x' * 10); sys.stdout.flush(); time.sleep(30)"


def test_new_stream_of_a_file_replaces_the_old_one(monkeypatch):
    t = Transcoder(ffmpeg="ffmpeg", ffprobe="ffprobe")
    monkeypatch.setattr(t, "command", lambda *a: [sys.executable, "-c", FAKE_FFMPEG])
    first = t.stream("http://x/a")
    assert next(first) == b"x" * 10
    second = t.stream("http://x/a")  # the player seeked
    assert list(first) == []  # killed: its slot is free again
    assert next(second) == b"x" * 10
    second.close()
    assert t._slots._value == transcode.MAX_SESSIONS and not t._active


def test_stream_slot_is_released_without_reading_and_when_busy(monkeypatch):
    t = Transcoder(ffmpeg="ffmpeg", ffprobe="ffprobe")
    monkeypatch.setattr(t, "command", lambda *a: [sys.executable, "-c", FAKE_FFMPEG])
    monkeypatch.setattr(transcode, "SLOT_WAIT", 0.1)
    streams = [t.stream(f"http://x/{i}") for i in range(transcode.MAX_SESSIONS)]
    with pytest.raises(TranscoderBusy):
        t.stream("http://x/other")
    for stream in streams:
        stream.close()  # what the WSGI server does, even for a body never read
    assert t._slots._value == transcode.MAX_SESSIONS

    monkeypatch.setattr(t, "command", lambda *a: ["/nonexistent/ffmpeg"])
    with pytest.raises(OSError):
        t.stream("http://x/broken")
    assert t._slots._value == transcode.MAX_SESSIONS


# -- AI subtitles ----------------------------------------------------------------------


class _FakeTranscoder:
    available = True
    ffmpeg = "ffmpeg"


def test_failed_audio_extract_is_an_error_not_an_empty_window(monkeypatch):
    t = Transcriber(_FakeTranscoder(), "", "http://127.0.0.1:1/v1")
    failed = subprocess.CompletedProcess([], 1, b"", b"Connection refused")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: failed)
    with pytest.raises(TranscribeError) as error:
        t.extract_audio("http://x/stream", 0, 30)
    assert error.value.status == 504 and error.value.retry_after


def test_failed_translation_is_not_cached(monkeypatch):
    t = Transcriber(_FakeTranscoder(), "", "http://127.0.0.1:1/v1")
    made = []
    monkeypatch.setattr(t, "extract_audio", lambda *a: made.append(1) or b"x" * 4000)
    segment = {"start": 1.0, "end": 3.0, "text": "Hello there."}
    monkeypatch.setattr(
        t, "transcribe", lambda *a, **k: {"segments": [segment], "language": "en"}
    )

    class Translator:
        works = False

        def translate(self, texts, target, engine):
            if not self.works:
                raise RuntimeError("429")
            return [f"[{target}] {text}" for text in texts]

    t.translator = Translator()
    args = ("http://x/stream", 0.0, 30.0)
    assert not t.window(*args, translate_to="cs", language="en")["translated"]
    t.translator.works = True
    result = t.window(*args, translate_to="cs", language="en")
    assert result["translated"] and result["lines"][0]["text"].startswith("[cs]")
    t.window(*args, translate_to="cs", language="en")
    assert len(made) == 2  # the translated window came from the cache


# -- subtitle files --------------------------------------------------------------------


def test_subtitle_downloaded_once_for_parallel_requests(tmp_path, monkeypatch):
    service = SubtitleService(torrserver=None, cache_dir=str(tmp_path))
    downloads = []

    def download(candidate_id):
        downloads.append(candidate_id)
        time.sleep(0.2)
        return VTT

    monkeypatch.setattr(service, "_download_vtt", download)
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(service.get_vtt("os-1")))
        for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [VTT] * 3 and downloads == ["os-1"]
    assert [p.suffix for p in tmp_path.iterdir()] == [".vtt"]  # no temporary file left
