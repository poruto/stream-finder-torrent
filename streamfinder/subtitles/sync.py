"""Align subtitles to the speech in the audio track - only when the match is certain.

Subtitles that are off by a constant offset (different cut, studio logo) or
drift (made for a 25 fps PAL release while the video is 23.976 fps) are fixed
by finding the transform `t' = t * scale + offset`. A release cut differently
(recap missing, ad breaks) needs another offset after the cut: the mapping is
then made of `Piece`s, one per measured stretch, joined by straight lines.

Two ways to measure a stretch, best first:

* `match_words` + `fit_pairs`: with AI subtitles configured, the spoken words
  are transcribed with their timings and every subtitle line is looked for
  where its first words are said. Many lines at the same shift leave no doubt,
  the median of their shifts is exact to a few hundredths of a second, and a
  jump of the shift inside the stretch shows a cut.
* `align`: without a transcript, subtitle cues are laid over speech detected by
  ffmpeg (silencedetect), then the line starts are pulled onto the starts of
  speech after a pause. Accepted only with a clear, unique best shift that a
  second, separate stretch of the film confirms (`confirm`).

Anything less than certain changes nothing: wrong subtitles are worse than late ones.
"""

import bisect
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from statistics import median

# Frame-rate conversions that commonly cause drift (PAL speed-up and back).
SCALES = (1.0, 25 / 23.976, 23.976 / 25)
MAX_SHIFT = 12.0  # seconds
STEP = 0.1
FINE_STEP = 0.02
MIN_CONFIDENCE = 0.55  # share of subtitle time that must land on speech
MIN_GAIN = 0.10  # agreement gained over leaving the subtitles as they are
MIN_MARGIN = 0.06  # the best shift's agreement must beat every clearly different shift's by this much
AGREE = 0.3  # seconds two independent measurements may differ by

# Line starts against speech starts (the fine part of `align`)
ONSET_GAP = 0.3  # seconds of quiet before a start that counts as one
ONSET_REACH = (0.6, 0.25)  # how far a line start may be from its speech start, per pass
MIN_ONSETS = 6

# Text matching
MIN_SIMILARITY = 0.75
MIN_WORDS = 2
HEAD = 5  # words from the start of a line looked for in the transcript
MIN_PAIRS = 4  # matched lines that must agree on one piece
CLUSTER = 0.6  # seconds: lines agreeing within this are the same shift
INLIER = 0.35  # seconds a matched line may be off its piece and still belong to it
MIN_SPREAD = 20.0  # seconds of film the agreeing lines must cover (more than one place)
MAX_PIECES = 3  # pieces one listened stretch may split into
MIN_SHIFT = 0.15  # smaller corrections are not worth applying
SEGMENT_LEAD = 0.25  # Whisper starts segments a little before the voice does


@dataclass
class Piece:
    """A stretch of the subtitle file (its own times) that maps by `t * scale + offset`."""

    start: float
    end: float
    scale: float
    offset: float

    def at(self, t: float) -> float:
        return t * self.scale + self.offset

    def to_dict(self) -> dict:
        return {
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "scale": round(self.scale, 6),
            "offset": round(self.offset, 3),
        }


@dataclass
class SyncResult:
    offset: float
    scale: float
    confidence: float
    baseline: float
    applied: bool
    method: str = "speech"
    matches: int = 0
    details: dict = field(default_factory=dict)
    pieces: list[Piece] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "offset": self.offset,
            "scale": self.scale,
            "confidence": self.confidence,
            "baseline": self.baseline,
            "applied": self.applied,
            "method": self.method,
            "matches": self.matches,
            "pieces": [p.to_dict() for p in self.pieces],
        }


# -- the mapping ------------------------------------------------------------------


def _agrees(a: Piece, b: Piece) -> bool:
    return a.scale == b.scale and abs(a.offset - b.offset) <= AGREE


def merge(pieces: list[Piece], new: Piece) -> list[Piece]:
    """Add a fresh measurement to the earlier ones of the same subtitle.

    Earlier pieces it overlaps but disagrees with are dropped (the fresh one is
    as certain and newer); neighbours that agree become one piece with the
    fresh offset, so the mapping only splits where the film really is cut.
    """
    kept = [
        p
        for p in pieces
        if _agrees(p, new) or not (p.start < new.end and new.start < p.end)
    ]
    merged: list[Piece] = []
    for p in sorted([*kept, new], key=lambda p: p.start):
        last = merged[-1] if merged else None
        if last is not None and _agrees(last, p):
            fresh = p is new or last is new
            source = new if fresh else last
            merged[-1] = Piece(
                min(last.start, p.start),
                max(last.end, p.end),
                source.scale,
                source.offset,
            )
            if fresh:
                new = merged[-1]
            continue
        merged.append(p)
    return merged


def mapped(pieces: list[Piece], t: float) -> float:
    """Where subtitle time `t` belongs in the video (same as the player's mapTime).

    Inside a piece (or before the first, after the last) its own transform;
    between two pieces a straight line from one to the other - exact for drift,
    and where a cut is, there usually is no dialogue anyway.
    """
    if not pieces:
        return t
    i = max(0, bisect.bisect_right([p.start for p in pieces], t) - 1)
    p = pieces[i]
    if t < p.start or t <= p.end or i == len(pieces) - 1:
        return p.at(t)
    q = pieces[i + 1]
    a, b = p.at(p.end), q.at(q.start)
    return a + (t - p.end) / (q.start - p.end) * (b - a)


def nearest(pieces: list[Piece], position: float) -> Piece | None:
    """The piece that applies at `position` (video time), or the closest one."""

    def distance(p: Piece) -> float:
        return max(p.at(p.start) - position, position - p.at(p.end), 0.0)

    return min(pieces, key=distance, default=None)


# -- speech detection -----------------------------------------------------------------


def _overlap(
    intervals: list[tuple[float, float]], speech: list[tuple[float, float]]
) -> float:
    """Total overlap of two sorted interval lists (linear merge)."""
    total, j, last = 0.0, 0, len(speech) - 1
    if last < 0:
        return total
    s2, e2 = speech[0]
    for s1, e1 in intervals:
        while True:
            start = s1 if s1 > s2 else s2
            end = e1 if e1 < e2 else e2
            if end > start:
                total += end - start
            if e1 < e2:
                break  # next interval
            if j == last:
                return total
            j += 1
            s2, e2 = speech[j]
    return total


def in_reach(
    cues: list[tuple[float, float]], window: tuple[float, float]
) -> list[tuple[float, float]]:
    """Cues that some shift and scale `align` tries can move into the window.

    The others never count, and dropping the rest of the film up front makes
    the ~700 tries fast on a Raspberry Pi.
    """
    lo, hi = window
    reach = MAX_SHIFT + 1  # the fine search goes a little past MAX_SHIFT
    return [
        (s, e)
        for s, e in cues
        if max(e * k for k in SCALES) + reach > lo
        and min(s * k for k in SCALES) - reach < hi
    ]


def _onset_shift(cues, speech, scale, offset, window, reach) -> float | None:
    """Median distance from line starts to the speech starts they belong to.

    Only starts after a pause count, on both sides: there the voice begins
    audibly and the subtitle author starts the line. Too few of them: None.
    """
    lo, hi = window
    onsets = [
        s
        for i, (s, _) in enumerate(speech)
        if s > lo + 0.05 and (i == 0 or s - speech[i - 1][1] >= ONSET_GAP)
    ]
    if not onsets:
        return None
    diffs = []
    for i, (s, _) in enumerate(cues):
        if i and s - cues[i - 1][1] < ONSET_GAP:
            continue
        t = s * scale + offset
        if not lo < t < hi:
            continue
        k = bisect.bisect_left(onsets, t)
        near = min(onsets[max(0, k - 1) : k + 1], key=lambda o: abs(o - t))
        if abs(near - t) <= reach:
            diffs.append(near - t)
    return median(diffs) if len(diffs) >= MIN_ONSETS else None


def align(
    cues: list[tuple[float, float]],
    speech: list[tuple[float, float]],
    window: tuple[float, float],
) -> SyncResult:
    """Best (offset, scale) for `cues` (seconds) against `speech` detected inside `window`.

    A shift is judged by how much of the window subtitles and speech agree on
    (both on or both off): that punishes lines over silence and speech without
    lines alike, and so picks the right shift even in dense dialogue, where
    nearly every shift puts most lines on some speech. `confidence` and
    `baseline` are the share of subtitle time on speech, after and before.
    """
    lo, hi = window
    speech = [(max(s, lo), min(e, hi)) for s, e in sorted(speech) if e > lo and s < hi]
    cues = sorted(in_reach(cues, window))
    speech_total = sum(e - s for s, e in speech)
    # Scaled once per scale, not on every try; sorted by start, so a try only
    # looks at the cues that can reach into the window (found by bisection).
    scaled = {k: [(s * k, e * k) for s, e in cues] for k in SCALES}
    starts = {k: [s for s, _ in moved] for k, moved in scaled.items()}
    longest = {
        k: max((e - s for s, e in moved), default=0.0) for k, moved in scaled.items()
    }

    def measure(scale: float, offset: float) -> tuple[float, float]:
        """(agreement, share of subtitle time on speech)."""
        first = bisect.bisect_left(starts[scale], lo - offset - longest[scale] - 1e-6)
        last = bisect.bisect_right(starts[scale], hi - offset + 1e-6, first)
        inside = [
            (lo if s < lo else s, hi if e > hi else e)
            for s, e in ((s + offset, e + offset) for s, e in scaled[scale][first:last])
            if e > lo and s < hi
        ]
        length = sum(e - s for s, e in inside)
        # A handful of cues brushing the window edge must not "match" perfectly.
        if length < 0.3 * speech_total or length <= 1:
            return 0.0, 0.0
        both = _overlap(inside, speech)
        return (hi - lo - length - speech_total + 2 * both) / (hi - lo), both / length

    base_agreement, baseline = measure(1.0, 0.0)
    steps = int(MAX_SHIFT / STEP)
    curve = [
        (measure(scale, k * STEP)[0], scale, k * STEP)
        for scale in SCALES
        for k in range(-steps, steps + 1)
    ]
    agreement, scale, offset = max(curve, key=lambda c: c[0])
    # Refine around the coarse peak (k = 0 is the peak itself).
    for k in range(-int(STEP / FINE_STEP), int(STEP / FINE_STEP) + 1):
        if not k:
            continue
        value = measure(scale, offset + k * FINE_STEP)[0]
        if value > agreement + 1e-9:
            agreement, offset = value, offset + k * FINE_STEP
    # The peak is flat to a tenth of a second or so (lines stay up after the voice
    # stops): the exact shift comes from where lines and speech start, if that
    # keeps the agreement.
    for reach in ONSET_REACH:
        shift = _onset_shift(cues, speech, scale, offset, window, reach)
        if shift is not None and measure(scale, offset + shift)[0] >= agreement - 0.03:
            offset += shift
    confidence = measure(scale, offset)[1]
    # A clear peak: nothing far from it (other scale, or > 1.5 s away) comes close.
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


def confirm(first: SyncResult, second: SyncResult) -> SyncResult:
    """Accept a speech-based shift only if another stretch of the film agrees."""
    agrees = (
        first.applied
        and second.applied
        and first.scale == second.scale
        and abs(first.offset - second.offset) <= AGREE
    )
    if agrees:
        offset = round((first.offset + second.offset) / 2, 3)
        confidence = round(min(first.confidence, second.confidence), 3)
        both = first.pieces + second.pieces
        piece = Piece(
            min(p.start for p in both), max(p.end for p in both), first.scale, offset
        )
        return SyncResult(
            round(offset, 2),
            first.scale,
            confidence,
            first.baseline,
            True,
            pieces=[piece] if both else [],
        )
    return SyncResult(
        first.offset, first.scale, first.confidence, first.baseline, False
    )


# -- text matching --------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>|\{[^}]*\}|\[[^\]]*\]|\([^)]*\)|♪")
_WORD_RE = re.compile(r"[^\W_]+")
_SPECIAL_TOKEN = re.compile(r"\s*(\[_[A-Z]+_?\d*\]|<\|[^|]*\|>)\s*")  # whisper.cpp


@lru_cache(maxsize=16384)  # a film's lines and the words heard recur on every sync
def _words(text: str) -> str:
    text = _TAG_RE.sub(" ", text or "")
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(_WORD_RE.findall(text))


Word = tuple[float, str, bool]  # (time in the file, normalized word, start time known)


def _add(out: list[Word], t: float, text: str, timed: bool) -> None:
    """One spoken word, split like the subtitle text ("don't" -> "don", "t")."""
    for i, part in enumerate(_words(text).split()):
        out.append((t, part, timed and i == 0))


def spoken_words(data: dict, offset: float) -> list[Word]:
    """Every word of a Whisper answer (verbose_json) with its time in the file.

    Groq/OpenAI send one word list with start times in seconds. whisper.cpp
    (started with --dtw) puts tokens in each segment with `t_dtw` in centiseconds;
    a token starting with a space starts a word. Without either, only the
    segment start is known, and only its first word counts as timed.
    """
    out: list[Word] = []
    segments = data.get("segments") or []
    dtw = any(
        (w.get("t_dtw") or -1) >= 0 for seg in segments for w in seg.get("words") or []
    )
    if data.get("words") and not dtw:
        for w in data["words"]:
            _add(out, offset + float(w.get("start") or 0), str(w.get("word")), True)
        return out
    for seg in segments:
        tokens = [
            w
            for w in seg.get("words") or []
            if not _SPECIAL_TOKEN.fullmatch(str(w.get("word") or ""))
        ]
        if any((w.get("t_dtw") or -1) >= 0 for w in tokens):
            word, t = "", -1.0
            for w in tokens:
                text = str(w.get("word") or "")
                if word and text.startswith(" "):
                    _add(out, offset + t / 100, word, t >= 0)
                    word = ""
                if not word:
                    t = float(w.get("t_dtw") if w.get("t_dtw") is not None else -1)
                word += text
            if word:
                _add(out, offset + t / 100, word, t >= 0)
            continue
        start = offset + float(seg.get("start") or 0) + SEGMENT_LEAD
        _add(out, start, str(seg.get("text") or ""), True)
    return out


def line_words(lines: list[tuple[float, float, str]]) -> list[Word]:
    """Words of transcript lines that only have a start time each."""
    out: list[Word] = []
    for start, _, text in sorted(lines):
        _add(out, start, text, True)
    return out


def _similarity(threshold: float) -> Callable[[str, str], float]:
    """`SequenceMatcher(None, a, b).ratio()`, worked out once per pair of texts.

    Pairs that certainly stay under `threshold` give 0.0 without it: the length
    of the shorter text and the letters both share (SequenceMatcher's own
    real_quick_ratio / quick_ratio bounds) rule most of them out cheaply.
    """
    cache: dict[tuple[str, str], float] = {}
    letters: dict[str, Counter] = {}

    def ratio(a: str, b: str) -> float:
        total = len(a) + len(b)
        if not total:
            return 1.0
        if 2.0 * min(len(a), len(b)) / total < threshold:
            return 0.0
        if a not in letters:
            letters[a] = Counter(a)
        if b not in letters:
            letters[b] = Counter(b)
        if 2.0 * sum((letters[a] & letters[b]).values()) / total < threshold:
            return 0.0
        return SequenceMatcher(None, a, b).ratio()

    def similarity(a: str, b: str) -> float:
        value = cache.get((a, b))
        if value is None:
            value = cache[a, b] = ratio(a, b)
        return value

    return similarity


def match_words(
    cues: list[tuple[float, float, str]], words: list[Word]
) -> list[tuple[float, float, float]]:
    """(line start in the subtitle, time its first word is said, similarity).

    A line is looked for wherever its first word is heard within reach of the
    largest shift we accept; the next few words must match too. A line can
    match in several places ("No, no, no."): `fit_pairs` keeps the one that
    agrees with the others.

    Lines out of reach of every word heard are skipped before their text is
    looked at, and the same words compared again (a film repeats its words
    and names) cost a dictionary lookup.
    """
    words = sorted(words, key=lambda w: w[0])  # stable: a line's words keep their order
    if not words:
        return []
    times = [t for t, _, _ in words]
    said = [w for _, w, _ in words]
    reach = MAX_SHIFT + 1
    first_like = _similarity(0.8)
    phrase_like = _similarity(MIN_SIMILARITY)
    pairs = []
    for start, _, text in cues:
        spans = [
            (
                bisect.bisect_left(times, start * k - reach),
                bisect.bisect_right(times, start * k + reach),
            )
            for k in SCALES
        ]
        if all(lo >= hi for lo, hi in spans):
            continue
        head = _words(text).split()[:HEAD]
        if len(head) < MIN_WORDS:
            continue
        phrase = " ".join(head)
        sizes = [n for n in (len(head) - 1, len(head), len(head) + 1) if n >= 1]
        seen = set()
        for lo, hi in spans:
            for i in range(lo, hi):
                t, word, timed = words[i]
                if i in seen or not timed:
                    continue
                seen.add(i)
                first = word == head[0] or (
                    len(head[0]) > 3 and first_like(word, head[0]) >= 0.8
                )
                if not first:
                    continue
                similarity = max(
                    phrase_like(phrase, " ".join(said[i : i + n])) for n in sizes
                )
                if similarity >= MIN_SIMILARITY:
                    pairs.append((start, t, similarity))
    return pairs


def _densest(
    items: list[tuple[float, float, float]],
) -> list[tuple[float, float, float]]:
    """Items (x, residual, weight) of the CLUSTER-wide residual window with most weight."""
    items = sorted(items, key=lambda p: p[1])
    j, best, top = 0, (0, 0), 0.0
    weight = 0.0
    for i, item in enumerate(items):
        weight += item[2]
        while item[1] - items[j][1] > CLUSTER:
            weight -= items[j][2]
            j += 1
        if weight > top + 1e-9:
            top, best = weight, (j, i + 1)
    return items[best[0] : best[1]]


def _one_per_line(items, centre):
    """A line matched in several places counts once, where it fits best."""
    best: dict[float, tuple[float, float, float]] = {}
    for x, r, w in items:
        if x not in best or abs(r - centre) < abs(best[x][1] - centre):
            best[x] = (x, r, w)
    return sorted(best.values())


def _pieces(pairs, scale: float) -> list[tuple[Piece, int]]:
    """Split matched lines into stretches with one shift each (most lines first)."""
    remaining = [(x, y - x * scale, w) for x, y, w in pairs]
    found: list[tuple[Piece, int]] = []
    while remaining and len(found) < MAX_PIECES:
        cluster = _densest(remaining)
        if not cluster:
            break
        centre = median(r for _, r, _ in cluster)
        inliers = _one_per_line(
            [p for p in remaining if abs(p[1] - centre) <= INLIER], centre
        )
        if len(inliers) < MIN_PAIRS:
            break
        offset = median(r for _, r, _ in inliers)
        inliers = [p for p in inliers if abs(p[1] - offset) <= INLIER]
        if len(inliers) < MIN_PAIRS:
            break
        offset = median(r for _, r, _ in inliers)
        x0, x1 = inliers[0][0], inliers[-1][0]
        # Lines on both sides of an earlier piece: not one stretch, a coincidence.
        if any(x0 < p.start and p.end < x1 for p, _ in found):
            break
        found.append((Piece(x0, x1, scale, offset), len(inliers)))
        remaining = [p for p in remaining if not x0 <= p[0] <= x1]
    return found


def fit_pairs(pairs: list[tuple[float, float, float]]) -> SyncResult:
    """The mapping that puts matched lines where they are said, if it is certain.

    Certain means: enough lines agree on each piece, they come from more than
    one place in the film, and most matched lines fit some piece.
    """
    lines = len({x for x, _, _ in pairs})
    best: tuple[int, float, list[tuple[Piece, int]]] | None = None
    for scale in SCALES:
        found = _pieces(pairs, scale)
        support = sum(n for _, n in found)
        if found and (best is None or support > best[0]):
            best = (support, scale, found)
    if best is None:
        return SyncResult(0.0, 1.0, 0.0, 0.0, False, method="text", details={})

    support, scale, found = best
    main, count = found[0]
    agreement = support / lines
    sure = (
        count >= MIN_PAIRS and main.end - main.start >= MIN_SPREAD and agreement >= 0.6
    )
    pieces = sorted((p for p, _ in found), key=lambda p: p.start)
    if len(pieces) == 1 and scale == 1.0 and abs(main.offset) < MIN_SHIFT:
        pieces[0].offset = 0.0  # already right: within what subtitle authors vary by
    changed = any(p.scale != 1.0 or p.offset for p in pieces)
    applied = sure and changed
    return SyncResult(
        round(main.offset, 2),
        round(scale, 6),
        round(agreement, 3),
        1.0 if sure and not applied else 0.0,
        applied,
        method="text",
        matches=support,
        details={"sure": sure},
        pieces=pieces if sure else [],
    )


def align_text(
    cues: list[tuple[float, float, str]],
    lines: list[tuple[float, float, str]],
) -> SyncResult:
    """Shift that makes subtitle lines start where the same words are spoken.

    `cues`: subtitle (start, end, text) as in the file; `lines`: transcript of
    the audio (start, end, text) in file time. Both must be in the same language.
    """
    return fit_pairs(match_words(cues, line_words(lines)))
