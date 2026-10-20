"""Timing of subtitle sync on a synthetic full film (not collected by pytest).

    python tests/bench_sync.py [repeats]

A film of 1500 subtitle lines is matched with what Whisper heard: the words of
the whole film (worst case) and of 150 s around one place (what the app
listens to), shifted, with PAL drift and with a cut, a tenth of it noise.
Prints the best of `repeats` cold runs per case and the result, so a speed-up
can be checked against the same answers.
"""

import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TMDB_API_KEY", "bench")

from streamfinder.subtitles import sync  # noqa: E402
from streamfinder.subtitles.sync import align, fit_pairs, match_words  # noqa: E402

VOCABULARY = (
    "where are you going tonight told have work late always say that this time "
    "true promise then call when get there will don't wait for did hear noise "
    "outside only wind back sleep what we need now who knows the door open come "
    "here look at me never again money car gun police run fast stop it listen "
    "please help him her they our house city night morning tomorrow yesterday "
    "kill find tell know think want go see take make give keep leave"
)
WORDS = VOCABULARY.split()


def film(seed=1, cues=1500, offset=2.3, scale=1.0, cut=None, noise=0.1):
    """(subtitle cues, heard words) for one synthetic film.

    The words of each line are said at `t * scale + offset` (plus `cut[1]`
    seconds after `cut[0]`), spread over the line, a little jittered; a `noise`
    share of words is misheard and as many lines are not heard at all.
    """
    rnd = random.Random(seed)
    subtitle, heard, t = [], [], 5.0
    for _ in range(cues):
        length = rnd.uniform(1.0, 4.0)
        line = [rnd.choice(WORDS) for _ in range(rnd.randint(2, 9))]
        subtitle.append((t, t + length, " ".join(line).capitalize() + "."))
        at = t * scale + offset + (cut[1] if cut and t >= cut[0] else 0.0)
        if rnd.random() >= noise:
            for i, word in enumerate(line):
                if rnd.random() < noise:
                    word = rnd.choice(WORDS)  # misheard
                said = at + i * length * scale / len(line) + rnd.gauss(0, 0.03)
                heard.append((said, word, True))
        t += length + rnd.uniform(0.3, 3.0)
    return subtitle, sorted(heard)


def listened(words, at, length=150.0):
    """The words of five 30 s windows around `at`, as the app transcribes them."""
    return [w for w in words if at <= w[0] < at + length]


def speech_case(seed=1, cues=1500, offset=3.2):
    """Cues of a whole film and speech detected in a 120 s window."""
    rnd = random.Random(seed)
    t, intervals = 0.0, []
    for _ in range(cues):
        talk = rnd.uniform(1.0, 4.0)
        intervals.append((t, t + talk))
        t += talk + rnd.uniform(0.4, 2.5)
    lo = intervals[len(intervals) // 2][0]
    window = (lo, lo + 120)
    speech = [(s, e) for s, e in intervals if s < window[1] and e > window[0]]
    return [(s - offset, e - offset) for s, e in intervals], speech, window


CASES = {
    "offset +2.3 s": {"offset": 2.3},
    "PAL drift": {"offset": 0.4, "scale": 25 / 23.976},
    "cut at 1h (+4 s)": {"offset": 1.1, "cut": (3600.0, 4.0)},
}


def best_of(repeats, fn, *args):
    times, result = [], None
    for _ in range(repeats):
        cache_clear = getattr(sync._words, "cache_clear", None)
        if cache_clear:  # time a cold run, as the first sync of a film is
            cache_clear()
        start = time.perf_counter()
        result = fn(*args)
        times.append(time.perf_counter() - start)
    return min(times), result


def sync_text(cues, words):
    return fit_pairs(match_words(cues, words))


def main(repeats=3):
    for name, kwargs in CASES.items():
        cues, words = film(**kwargs)
        for part, heard in (
            ("whole film", words),
            ("150 s at 1h", listened(words, 3600.0)),
        ):
            took, r = best_of(repeats, sync_text, cues, heard)
            print(
                f"text   {name:17} {part:11} {took * 1000:8.1f} ms  "
                f"offset={r.offset} scale={r.scale} matches={r.matches} "
                f"agreement={r.confidence} sure={r.details.get('sure')} "
                f"pieces={len(r.pieces)}"
            )
    cues, speech, window = speech_case()
    took, r = best_of(repeats, align, cues, speech, window)
    print(
        f"speech {'offset +3.2 s':17} {'120 s':11} {took * 1000:8.1f} ms  "
        f"offset={r.offset} scale={r.scale} confidence={r.confidence} "
        f"applied={r.applied}"
    )


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 3)
