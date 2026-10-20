import random

from streamfinder.subtitles.sync import align


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
