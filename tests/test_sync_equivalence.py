"""The fast subtitle sync gives the same answers as the original, slower code.

`_old_match_words` and `_old_align` are frozen copies of the code before the
speed-up (every word in reach compared afresh, offsets re-scaled on every try).
Many seeded random cases - shifts, PAL drift, cuts, misheard words, lines not
heard, repeated lines, unrelated films, whole films and short stretches - must
give the same matched pairs, the same mapping (within 1 ms) and the same
"certain" decision.
"""

import bisect
import random
from difflib import SequenceMatcher

import pytest

from streamfinder.subtitles.sync import (
    FINE_STEP,
    HEAD,
    MAX_SHIFT,
    MIN_CONFIDENCE,
    MIN_GAIN,
    MIN_MARGIN,
    MIN_SIMILARITY,
    MIN_WORDS,
    ONSET_REACH,
    SCALES,
    STEP,
    Piece,
    SyncResult,
    _onset_shift,
    _words,
    align,
    fit_pairs,
    in_reach,
    match_words,
)

# -- frozen copies of the original code -----------------------------------------


def _old_match_words(cues, words):
    words = sorted(words, key=lambda w: w[0])
    if not words:
        return []
    times = [t for t, _, _ in words]
    reach = MAX_SHIFT + 1
    pairs = []
    for start, _, text in cues:
        head = _words(text).split()[:HEAD]
        if len(head) < MIN_WORDS:
            continue
        phrase = " ".join(head)
        seen = set()
        for k in SCALES:
            lo = bisect.bisect_left(times, start * k - reach)
            hi = bisect.bisect_right(times, start * k + reach)
            for i in range(lo, hi):
                t, word, timed = words[i]
                if i in seen or not timed:
                    continue
                seen.add(i)
                first = word == head[0] or (
                    len(head[0]) > 3
                    and SequenceMatcher(None, word, head[0]).ratio() >= 0.8
                )
                if not first:
                    continue
                similarity = max(
                    SequenceMatcher(
                        None, phrase, " ".join(w for _, w, _ in words[i : i + n])
                    ).ratio()
                    for n in (len(head) - 1, len(head), len(head) + 1)
                    if n >= 1
                )
                if similarity >= MIN_SIMILARITY:
                    pairs.append((start, t, similarity))
    return pairs


def _old_overlap(intervals, speech):
    total, i, j = 0.0, 0, 0
    while i < len(intervals) and j < len(speech):
        start = max(intervals[i][0], speech[j][0])
        end = min(intervals[i][1], speech[j][1])
        if end > start:
            total += end - start
        if intervals[i][1] < speech[j][1]:
            i += 1
        else:
            j += 1
    return total


def _old_align(cues, speech, window):
    lo, hi = window
    speech = [(max(s, lo), min(e, hi)) for s, e in sorted(speech) if e > lo and s < hi]
    cues = sorted(in_reach(cues, window))
    speech_total = sum(e - s for s, e in speech)

    def measure(scale, offset):
        moved = [(s * scale + offset, e * scale + offset) for s, e in cues]
        inside = [(max(s, lo), min(e, hi)) for s, e in moved if e > lo and s < hi]
        length = sum(e - s for s, e in inside)
        if length < 0.3 * speech_total or length <= 1:
            return 0.0, 0.0
        both = _old_overlap(inside, speech)
        return (hi - lo - length - speech_total + 2 * both) / (hi - lo), both / length

    base_agreement, baseline = measure(1.0, 0.0)
    steps = int(MAX_SHIFT / STEP)
    curve = [
        (measure(scale, k * STEP)[0], scale, k * STEP)
        for scale in SCALES
        for k in range(-steps, steps + 1)
    ]
    agreement, scale, offset = max(curve, key=lambda c: c[0])
    for k in range(-int(STEP / FINE_STEP), int(STEP / FINE_STEP) + 1):
        value = measure(scale, offset + k * FINE_STEP)[0]
        if value > agreement + 1e-9:
            agreement, offset = value, offset + k * FINE_STEP
    for reach in ONSET_REACH:
        shift = _onset_shift(cues, speech, scale, offset, window, reach)
        if shift is not None and measure(scale, offset + shift)[0] >= agreement - 0.03:
            offset += shift
    confidence = measure(scale, offset)[1]
    rival = max(
        (value for value, s, o in curve if s != scale or abs(o - offset) > 1.5),
        default=0.0,
    )
    margin = agreement - rival
    applied = (
        confidence >= MIN_CONFIDENCE
        and agreement - base_agreement >= MIN_GAIN
        and margin >= MIN_MARGIN
    )
    offset = round(offset, 3)
    covered = Piece((lo - offset) / scale, (hi - offset) / scale, scale, offset)
    return SyncResult(
        round(offset, 2),
        round(scale, 6),
        round(confidence, 3),
        round(baseline, 3),
        applied,
        details={"margin": round(margin, 3), "agreement": round(agreement, 3)},
        pieces=[covered],
    )


# -- random cases ----------------------------------------------------------------

VOCABULARY = (
    "where are you going tonight told have work late always say that this time "
    "true promise then call when there will don't wait hear noise outside only "
    "wind back sleep listen carefully nobody knows about money need leave open "
    "door slowly who sent here remember what happened last summer brother car "
    "keys station no yes sir okay come on"
).split()
REPEATS = ["no no no", "yes sir", "come on come on", "okay okay", "thank you"]


def _text_case(seed):
    """Subtitle cues and the words heard: a stretch of the film or all of it."""
    rnd = random.Random(seed)
    count = rnd.randint(0, 200)
    offset = rnd.uniform(-MAX_SHIFT - 2, MAX_SHIFT + 2) * rnd.choice((0, 0.02, 1))
    scale = rnd.choice(SCALES) if rnd.random() < 0.4 else 1.0
    cut = (rnd.uniform(0, 600), rnd.uniform(-8, 8)) if rnd.random() < 0.3 else None
    noise = rnd.choice((0.0, 0.1, 0.3, 1.0))
    timed = rnd.random() < 0.8  # else only the first word of a segment is timed
    first = rnd.choice((0.0, 5.0, 600.0, 3000.0))
    cues, heard, t = [], [], first
    for _ in range(count):
        if rnd.random() < 0.08:
            line = rnd.choice(REPEATS).split()
        else:
            line = [rnd.choice(VOCABULARY) for _ in range(rnd.randint(1, 8))]
        length = rnd.uniform(0.6, 4.5)
        text = " ".join(line).capitalize() + rnd.choice((".", "?", "!", "..."))
        cues.append((t, t + length, text))
        at = t * scale + offset + (cut[1] if cut and t >= first + cut[0] else 0.0)
        if rnd.random() >= noise:  # else not heard, or another line is
            for i, word in enumerate(line):
                if rnd.random() < noise / 2:
                    word = rnd.choice(VOCABULARY)  # misheard
                said = at + i * length * scale / len(line) + rnd.gauss(0, 0.04)
                heard.append((said, word, timed or i == 0))
        elif rnd.random() < 0.5:
            heard += [(at + 0.3 * i, rnd.choice(VOCABULARY), True) for i in range(4)]
        t += length + rnd.uniform(0.0, 3.0)
    if rnd.random() < 0.6 and heard:  # five 30 s windows around one place
        start = rnd.uniform(heard[0][0], heard[-1][0])
        heard = [w for w in heard if start <= w[0] < start + 150]
    rnd.shuffle(heard)  # match_words sorts them itself
    if rnd.random() < 0.3:
        rnd.shuffle(cues)
    return cues, heard


def _same(new, old):
    assert abs(new.offset - old.offset) <= 0.001
    assert new.scale == old.scale
    assert new.applied == old.applied
    assert new.confidence == old.confidence
    assert new.baseline == old.baseline
    assert new.matches == old.matches
    assert new.details == old.details
    assert len(new.pieces) == len(old.pieces)
    for a, b in zip(new.pieces, old.pieces, strict=True):
        assert a.scale == b.scale and abs(a.offset - b.offset) <= 0.001
        assert abs(a.start - b.start) <= 0.001 and abs(a.end - b.end) <= 0.001


@pytest.mark.parametrize("chunk", range(10))
def test_text_matching_gives_the_original_pairs(chunk):
    for seed in range(chunk * 30, chunk * 30 + 30):
        cues, heard = _text_case(seed)
        pairs = match_words(cues, heard)
        assert pairs == _old_match_words(cues, heard)  # same pairs, same order
        _same(fit_pairs(pairs), fit_pairs(_old_match_words(cues, heard)))


def test_text_cases_cover_both_decisions():
    results = [fit_pairs(match_words(*_text_case(seed))) for seed in range(300)]
    sure = sum(bool(r.details.get("sure")) for r in results)
    applied = [r for r in results if r.applied]
    assert 30 < sure < 270 and 15 < len(applied) < sure
    assert {r.scale for r in applied} == {round(k, 6) for k in SCALES}
    assert any(len(r.pieces) > 1 for r in applied)  # cuts found too


def _speech_case(seed):
    rnd = random.Random(seed)
    t, speech = 0.0, []
    end = rnd.choice((300, 900, 4000))
    while t < end:
        talk = rnd.uniform(0.3, 4.5)
        speech.append((t, t + talk))
        t += talk + rnd.uniform(0.1, 3.0)
    offset = rnd.uniform(-MAX_SHIFT - 1, MAX_SHIFT + 1)
    scale = rnd.choice(SCALES)
    drop = rnd.choice((0.0, 0.2, 0.5))
    cues = [
        ((s - offset) / scale, (e - offset) / scale + rnd.uniform(0, 1.5))
        for s, e in speech
        if rnd.random() >= drop
    ]
    rnd.shuffle(cues)
    lo = rnd.uniform(0, end - 100)
    window = (lo, lo + rnd.choice((30, 120)))
    heard = [(s, e) for s, e in speech if rnd.random() > drop / 2]
    return cues, heard, window


@pytest.mark.parametrize("seed", range(40))
def test_speech_sync_matches_the_original(seed):
    cues, speech, window = _speech_case(seed)
    new, old = align(cues, speech, window), _old_align(cues, speech, window)
    _same(new, old)
    assert new == old


def test_speech_sync_edge_cases_match_the_original():
    for cues, speech, window in [
        ([], [(1, 2)], (0, 10)),
        ([(1, 2)], [], (0, 10)),
        ([(1, 2)], [(1, 2)], (0, 10)),
        ([(0, 5), (1, 2), (3, 9)], [(2, 4), (2, 3)], (0, 10)),
        ([(s, s + 1.0) for s in range(0, 200, 2)], [(30, 31)], (25, 145)),
        ([(s, s + 30.0) for s in range(0, 600, 7)], [(100, 200)], (90, 210)),
    ]:
        assert align(cues, speech, window) == _old_align(cues, speech, window)
