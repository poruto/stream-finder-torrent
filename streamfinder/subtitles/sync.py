"""Align subtitles to the speech in the audio track - only when the match is certain.

Subtitles that are off by a constant offset (different cut, studio logo) or
drift (made for a 25 fps PAL release while the video is 23.976 fps) are fixed
by finding the transform `t' = t * scale + offset`.

Two ways to find it, best first:

* `align_text`: with AI subtitles configured, the spoken words are transcribed
  and matched with the subtitle text. Many lines saying the same words at the
  same shift leave no doubt, and the shift is measured to a tenth of a second.
* `align`: without a transcript, subtitle cues are laid over speech detected by
  ffmpeg (silencedetect). Accepted only with a clear, unique best shift that a
  second, separate stretch of the film confirms (`confirm`).

Anything less than certain changes nothing: wrong subtitles are worse than late ones.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

# Frame-rate conversions that commonly cause drift (PAL speed-up and back).
SCALES = (1.0, 25 / 23.976, 23.976 / 25)
MAX_SHIFT = 12.0  # seconds
STEP = 0.1
FINE_STEP = 0.02
MIN_CONFIDENCE = 0.55  # share of subtitle time that must land on speech
MIN_GAIN = 0.10  # improvement over leaving the subtitles as they are
MIN_MARGIN = 0.06  # the best shift must beat every clearly different shift by this much
AGREE = 0.3  # seconds two independent measurements may differ by

# Text matching
MIN_SIMILARITY = 0.72
MIN_WORDS = 2
MIN_PAIRS = 3  # matched lines that must agree on one shift
CLUSTER = 0.6  # seconds: lines agreeing within this are the same shift
MIN_SHIFT = 0.15  # smaller corrections are not worth applying


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

    def to_dict(self) -> dict:
        return {
            "offset": self.offset,
            "scale": self.scale,
            "confidence": self.confidence,
            "baseline": self.baseline,
            "applied": self.applied,
            "method": self.method,
            "matches": self.matches,
        }


def _overlap(
    intervals: list[tuple[float, float]], speech: list[tuple[float, float]]
) -> float:
    """Total overlap of two sorted interval lists (linear merge)."""
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


def align(
    cues: list[tuple[float, float]],
    speech: list[tuple[float, float]],
    window: tuple[float, float],
) -> SyncResult:
    """Best (offset, scale) for `cues` (seconds) against `speech` detected inside `window`."""
    lo, hi = window
    speech = sorted(speech)
    cues = sorted(in_reach(cues, window))
    speech_total = sum(e - s for s, e in speech)

    def score(scale: float, offset: float) -> float:
        moved = [(s * scale + offset, e * scale + offset) for s, e in cues]
        inside = [(max(s, lo), min(e, hi)) for s, e in moved if e > lo and s < hi]
        length = sum(e - s for s, e in inside)
        # A handful of cues brushing the window edge must not "match" perfectly.
        if length < 0.3 * speech_total or length <= 1:
            return 0.0
        return _overlap(inside, speech) / length

    baseline = score(1.0, 0.0)
    steps = int(MAX_SHIFT / STEP)
    curve = [
        (score(scale, k * STEP), scale, k * STEP)
        for scale in SCALES
        for k in range(-steps, steps + 1)
    ]
    confidence, scale, offset = max(curve, key=lambda c: c[0])
    # Refine around the coarse peak.
    for k in range(-int(STEP / FINE_STEP), int(STEP / FINE_STEP) + 1):
        value = score(scale, offset + k * FINE_STEP)
        if value > confidence + 1e-9:
            confidence, offset = value, offset + k * FINE_STEP
    # A clear peak: nothing far from it (other scale, or > 1.5 s away) comes close.
    rival = max(
        (value for value, s, o in curve if s != scale or abs(o - offset) > 1.5),
        default=0.0,
    )
    margin = confidence - rival
    applied = (
        confidence >= MIN_CONFIDENCE
        and confidence - baseline >= MIN_GAIN
        and margin >= MIN_MARGIN
    )
    return SyncResult(
        round(offset, 2),
        round(scale, 6),
        round(confidence, 3),
        round(baseline, 3),
        applied,
        details={"margin": round(margin, 3)},
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
        offset = round((first.offset + second.offset) / 2, 2)
        confidence = round(min(first.confidence, second.confidence), 3)
        return SyncResult(offset, first.scale, confidence, first.baseline, True)
    return SyncResult(
        first.offset, first.scale, first.confidence, first.baseline, False
    )


# -- text matching --------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>|\{[^}]*\}|\[[^\]]*\]|\([^)]*\)|♪")
_WORD_RE = re.compile(r"[^\W_]+")


def _words(text: str) -> str:
    text = _TAG_RE.sub(" ", text or "")
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(_WORD_RE.findall(text))


def align_text(
    cues: list[tuple[float, float, str]],
    lines: list[tuple[float, float, str]],
) -> SyncResult:
    """Shift that makes subtitle lines start where the same words are spoken.

    `cues`: subtitle (start, end, text) as in the file; `lines`: transcript of
    the audio (start, end, text) in file time. Both must be in the same language.
    """
    spoken = [(s, _words(t)) for s, _, t in lines]
    spoken = [(s, w) for s, w in spoken if len(w.split()) >= MIN_WORDS]
    written = [(s, _words(t)) for s, _, t in cues]
    written = [(s, w) for s, w in written if len(w.split()) >= MIN_WORDS]
    if not spoken or not written:
        return SyncResult(0.0, 1.0, 0.0, 0.0, False, method="text")

    best: tuple[float, int, float, list[float]] | None = (
        None  # (support, pairs, scale, shifts)
    )
    for scale in SCALES:
        pairs: list[tuple[float, float]] = []  # (shift, similarity)
        for start, words in spoken:
            # A spoken line is compared with subtitles whose start, once scaled,
            # is within reach of the largest shift we accept.
            for cue_start, cue_words in written:
                shift = start - cue_start * scale
                if abs(shift) > MAX_SHIFT + 1:
                    continue
                similarity = SequenceMatcher(None, words, cue_words).ratio()
                if similarity >= MIN_SIMILARITY:
                    pairs.append((shift, similarity))
        if not pairs:
            continue
        pairs.sort()
        # Densest cluster of agreeing shifts (sliding window of CLUSTER seconds).
        j, top = 0, (0.0, 0, 0, 0)
        for i in range(len(pairs)):
            while pairs[i][0] - pairs[j][0] > CLUSTER:
                j += 1
            weight = sum(sim for _, sim in pairs[j : i + 1])
            if weight > top[0]:
                top = (weight, i - j + 1, j, i)
        weight, count, j, i = top
        shifts = [shift for shift, _ in pairs[j : i + 1]]
        if best is None or weight > best[0]:
            best = (weight, count, scale, shifts, len(pairs))
    if best is None:
        return SyncResult(0.0, 1.0, 0.0, 0.0, False, method="text")

    weight, count, scale, shifts, total = best
    shifts.sort()
    offset = shifts[len(shifts) // 2]  # median: robust to a line that starts early
    agreement = count / total
    sure = count >= MIN_PAIRS and agreement >= 0.6
    applied = sure and (abs(offset) >= MIN_SHIFT or scale != 1.0)
    return SyncResult(
        round(offset, 2),
        round(scale, 6),
        round(agreement, 3),
        1.0 if sure and not applied else 0.0,
        applied,
        method="text",
        matches=count,
        details={"sure": sure},
    )
