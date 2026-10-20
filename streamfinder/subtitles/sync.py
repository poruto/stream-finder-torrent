"""Align subtitles to the speech in the audio track.

Subtitles that are off by a constant offset (different cut, studio logo) or
drift (made for a 25 fps PAL release while the video is 23.976 fps) are fixed
by finding the transform `t' = t * scale + offset` under which subtitle cues
overlap detected speech the most. Speech comes from ffmpeg's silencedetect on
a short window of audio, so no numpy and no full download is needed.
"""

from dataclasses import dataclass

# Frame-rate conversions that commonly cause drift (PAL speed-up and back).
SCALES = (1.0, 25 / 23.976, 23.976 / 25)
MAX_SHIFT = 12.0  # seconds
STEP = 0.1
MIN_CONFIDENCE = 0.45  # share of subtitle time that must land on speech
MIN_GAIN = 0.08  # improvement over leaving the subtitles as they are


@dataclass
class SyncResult:
    offset: float
    scale: float
    confidence: float
    baseline: float
    applied: bool

    def to_dict(self) -> dict:
        return self.__dict__.copy()


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


def align(
    cues: list[tuple[float, float]],
    speech: list[tuple[float, float]],
    window: tuple[float, float],
) -> SyncResult:
    """Best (offset, scale) for `cues` (seconds) against `speech` detected inside `window`."""
    lo, hi = window
    speech = sorted(speech)

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
    best = (baseline, 1.0, 0.0)
    steps = int(MAX_SHIFT / STEP)
    for scale in SCALES:
        for k in range(-steps, steps + 1):
            # The scale already fixes frame-rate drift, so the remaining offset is small.
            offset = k * STEP
            value = score(scale, offset)
            if value > best[0] + 1e-9:
                best = (value, scale, offset)
    confidence, scale, offset = best
    applied = confidence >= MIN_CONFIDENCE and confidence - baseline >= MIN_GAIN
    return SyncResult(
        round(offset, 2),
        round(scale, 6),
        round(confidence, 3),
        round(baseline, 3),
        applied,
    )
