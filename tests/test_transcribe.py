from streamfinder.subtitles.sync import align, align_text, confirm
from streamfinder.subtitles.transcribe import (
    Line,
    Transcriber,
    api_url_for,
    clean_segments,
    merge_sentences,
    pick_speech_model,
)

LINES = [
    "Where are you going tonight?",
    "I told you, I have to work late.",
    "You always say that.",
    "This time it is true, I promise.",
    "Then call me when you get there.",
    "I will, don't wait for me.",
    "Did you hear that noise outside?",
    "It's only the wind, go back to sleep.",
]


def _cues(shift=0.0, scale=1.0, start=600.0):
    return [
        ((start + 5 * i) * scale + shift, (start + 5 * i + 3) * scale + shift, text)
        for i, text in enumerate(LINES)
    ]


def test_text_sync_finds_exact_shift():
    spoken = [
        (s + 0.15, e, text.lower().rstrip("?.")) for s, e, text in _cues()
    ]  # Whisper style text
    subtitle = _cues(shift=-2.4)  # subtitles 2.4 s early
    result = align_text(subtitle, spoken)
    assert result.applied and result.method == "text" and result.matches >= 3
    assert abs(result.offset - 2.55) < 0.2 and result.scale == 1.0


def test_text_sync_leaves_matching_subtitles_alone():
    result = align_text(_cues(), _cues(shift=0.05))
    assert not result.applied and result.details["sure"]


def test_text_sync_refuses_unrelated_text():
    other = [
        (s, e, "completely different words here " + str(i))
        for i, (s, e, _) in enumerate(_cues())
    ]
    result = align_text(_cues(shift=3), other)
    assert not result.applied and not result.details.get("sure")


def test_speech_sync_needs_a_clear_peak():
    # Perfectly regular speech: every 5 s shift fits equally well -> not sure -> nothing applied.
    speech = [(600 + 5 * i, 603 + 5 * i) for i in range(24)]
    cues = [(s - 5, e - 5) for s, e in speech]
    assert not align(cues, speech, (600, 720)).applied


def test_speech_sync_confirmation_must_agree():
    a = align([(1, 2)], [(1, 2)], (0, 10))
    a.applied, a.offset = True, 2.0
    b = align([(1, 2)], [(1, 2)], (0, 10))
    b.applied, b.offset, b.scale = True, 2.2, a.scale
    assert confirm(a, b).applied and abs(confirm(a, b).offset - 2.1) < 1e-6
    b.offset = 4.0
    assert not confirm(a, b).applied


def test_clean_segments_drops_hallucinations_and_offsets():
    segments = [
        {
            "start": 1.0,
            "end": 3.0,
            "text": " Hello there. ",
            "no_speech_prob": 0.01,
            "avg_logprob": -0.2,
        },
        {
            "start": 10.0,
            "end": 12.0,
            "text": "Thank you for watching!",
            "no_speech_prob": 0.9,
            "avg_logprob": -0.9,
        },
        {
            "start": 20.0,
            "end": 21.0,
            "text": "la la la la la la la",
            "compression_ratio": 3.1,
            "avg_logprob": -0.3,
        },
    ]
    lines = clean_segments(segments, 300.0, 330.0)
    assert [(line.start, line.text) for line in lines] == [(301.25, "Hello there.")]


def test_local_whisper_server_needs_no_key():
    class NoFfmpeg:
        available = True
        ffmpeg = "ffmpeg"

    t = Transcriber(NoFfmpeg(), "", "http://192.168.0.10:8000/v1/")
    assert (
        t.available
        and t.api_url == "http://192.168.0.10:8000/v1"
        and t._headers() == {}
    )
    assert api_url_for("gsk_abc") == "https://api.groq.com/openai/v1"
    assert (
        pick_speech_model({"Systran/faster-whisper-small", "tts-1"})
        == "Systran/faster-whisper-small"
    )
    assert (
        pick_speech_model({"whisper-large-v3", "whisper-large-v3-turbo"})
        == "whisper-large-v3-turbo"
    )


def test_pieces_of_one_sentence_become_one_line():
    lines = [
        Line(600.8, 602.9, "Step forward."),
        Line(604.4, 606.6, "I was told some of you are"),
        Line(606.6, 607.4, "unhappy with my plan."),
        Line(611.0, 612.0, "Who said"),
    ]
    merged = merge_sentences(lines)
    assert [(m.start, m.text) for m in merged] == [
        (600.8, "Step forward."),
        (604.4, "I was told some of you are unhappy with my plan."),
        (611.0, "Who said"),
    ]
    assert merged[1].end == 607.4
