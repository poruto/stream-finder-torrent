"""AI subtitles: speech of the playing file turned into text while it plays.

Two free sources, switched on the Settings page:
* "local": a Whisper server on this device or your PC - whisper.cpp's server
  (installed by install-whisper.sh, port 8178) or any OpenAI-compatible one
  (Speaches / faster-whisper-server). Offline, no account.
* "api": a Groq key (free account, no card) or an OpenAI key.
The key, when there is one, also powers the "AI" translation engine.

The player asks for the subtitles of one short window at a time (a few seconds
ahead of the playback position). ffmpeg cuts that window's audio out of the
TorrServer stream as 16 kHz mono FLAC, a Whisper model behind an
OpenAI-compatible API (Groq by default, OpenAI works too) transcribes it with
timestamps, and a chat model can translate the lines into the viewer's language.
Nothing heavy runs locally, so a Raspberry Pi or a phone can do it.
"""

import contextlib
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
from collections import OrderedDict
from dataclasses import dataclass

import requests

from .formats import LANGUAGE_NAMES

logger = logging.getLogger(__name__)

GROQ_URL = "https://api.groq.com/openai/v1"
LOCAL_URL = "http://127.0.0.1:8178/v1"  # whisper.cpp server from install-whisper.sh
LOCAL_URL = "http://127.0.0.1:8178/v1"  # whisper.cpp server from install-whisper.sh
OPENAI_URL = "https://api.openai.com/v1"
# Preferred models, best first; the first one the account offers is used.
SPEECH_MODELS = ("whisper-large-v3-turbo", "whisper-large-v3", "whisper-1")
CHAT_MODELS = (
    "llama-3.3-70b-versatile",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "gpt-4.1-mini",
    "gpt-4o-mini",
    "llama-3.1-8b-instant",
)
MAX_WINDOW = 60  # seconds of audio per request
# Each window is transcribed with this much audio before and after it: Whisper
# then hears whole sentences at the edges and times them right; a line belongs
# to the window it starts in, so nothing is cut in half or shown twice.
OVERLAP = 3.0
LEAD = 0.25  # Whisper starts lines a little before the voice does
# Languages small Whisper models write poorly but translate into English well:
# "auto" goes audio -> English (Whisper) -> target (Google) for these.
VIA_ENGLISH = {
    "ja",
    "ko",
    "zh",
    "yue",
    "th",
    "vi",
    "ar",
    "he",
    "fa",
    "hi",
    "ur",
    "bn",
    "ta",
    "te",
    "ml",
    "kn",
    "mr",
    "gu",
    "pa",
    "km",
    "lo",
    "my",
    "si",
    "ka",
    "hy",
    "am",
    "mn",
    "id",
    "ms",
    "tl",
    "jw",
    "su",
}
READING_SPEED = 0.07  # seconds per character a line stays up at most (+1.2 s)
CACHE_SIZE = 600  # windows kept in memory (a 2 h film in 30 s windows is 240)


class TranscribeError(Exception):
    """The speech API failed; `status` is the HTTP status to answer with."""

    def __init__(
        self, message: str, status: int = 502, retry_after: float | None = None
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass
class Line:
    start: float  # absolute seconds in the file
    end: float
    text: str

    def to_dict(self) -> dict:
        return {
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "text": self.text,
        }


def api_url_for(key: str, configured: str = "") -> str:
    """Groq keys start with gsk_, everything else is assumed to be OpenAI unless configured."""
    if configured:
        return configured.rstrip("/")
    return GROQ_URL if key.startswith("gsk_") else OPENAI_URL


def pick_speech_model(offered) -> str | None:
    """Best Whisper model on offer; local servers name theirs e.g. 'Systran/faster-whisper-small'."""
    preferred = next((m for m in SPEECH_MODELS if m in offered), None)
    return preferred or next(
        (m for m in sorted(offered) if "whisper" in m.lower()), None
    )


_SOUND_TAG = re.compile(r"[\[(♪*].*[\])♪*]|\W*")


def _word_times(seg: dict, words: list[dict]) -> tuple[float, float] | None:
    """(first word start, last word end) of a segment, if the speech engine timed words.

    whisper.cpp (started with --dtw) puts the words in each segment with `t_dtw`
    in centiseconds - accurate to about a tenth of a second. Groq/OpenAI return
    one top-level word list with start/end in seconds.
    """
    timed = [
        w["t_dtw"] / 100
        for w in seg.get("words") or []
        if (w.get("t_dtw") or -1) >= 0 and str(w.get("word", "")).strip(" -")
    ]
    if timed:
        return timed[0], timed[-1] + 0.5
    s0, s1 = float(seg.get("start") or 0), float(seg.get("end") or 0)
    inside = [w for w in words if s0 - 0.5 <= float(w.get("start", -1)) < s1]
    if inside:
        return float(inside[0]["start"]), float(
            inside[-1].get("end") or inside[-1]["start"]
        )
    return None


def clean_segments(
    segments: list[dict],
    offset: float,
    window_end: float,
    words: list[dict] | None = None,
) -> list[Line]:
    """Whisper segments -> subtitle lines, dropping the usual hallucinations.

    Whisper invents text for music and silence ("Thank you for watching!"); those
    segments come with a high no-speech probability or a very low log-probability.
    A line starts with its first spoken word when the engine timed words, else
    with the segment (which tends to begin a little before the voice).
    """
    lines: list[Line] = []
    for seg in segments:
        text = " ".join(str(seg.get("text") or "").split())
        text = text.removeprefix("- ").removeprefix(
            "-"
        )  # whisper.cpp marks speakers with a dash
        if not text or _SOUND_TAG.fullmatch(text):
            continue  # "[MUSIC PLAYING]", "(laughs)", "♪ ... ♪": not speech
        no_speech = float(seg.get("no_speech_prob") or 0)
        logprob = float(seg.get("avg_logprob") or 0)
        # Unsure lines are kept unless they look like invented text: muffled
        # speech has a low log-probability too, and a line is better than none.
        if no_speech > 0.6 and logprob < -0.7:
            continue
        if logprob < -1.5 or float(seg.get("compression_ratio") or 0) > 2.6:
            continue
        timed = _word_times(seg, words or [])
        if timed:
            start, end = offset + timed[0], offset + timed[1]
        else:
            start = offset + float(seg.get("start") or 0) + LEAD
            end = offset + float(seg.get("end") or 0)
        end = min(end, window_end)
        if end - start < 0.3:
            end = start + min(1.5, max(0.3, window_end - start))
        if lines and lines[-1].text == text and start - lines[-1].end < 1:
            lines[-1].end = end  # repeated line: one longer cue
            continue
        lines.append(Line(start, end, text))
    for i, line in enumerate(lines):
        # Keep a line up only as long as it takes to read it, never past the next one.
        longest = 1.2 + READING_SPEED * len(line.text)
        limit = lines[i + 1].start if i + 1 < len(lines) else line.end
        line.end = max(
            line.start + 0.8,
            min(max(line.end, line.start + 1.0), line.start + longest, limit),
        )
    return lines


class Transcriber:
    def __init__(
        self,
        transcoder,
        api_key: str = "",
        api_url: str = "",
        provider: str = "",
        enabled: bool = True,
    ):
        self.transcoder = transcoder
        self.session = requests.Session()
        self._cache: OrderedDict[tuple, dict] = OrderedDict()
        self._lock = threading.Lock()
        self._inflight: dict[tuple, threading.Event] = {}
        self._extract_slots = threading.BoundedSemaphore(
            2
        )  # ffmpeg is the heavy part on a Pi
        self._models: tuple[str, str | None] | None = None
        self.translator = None  # set by Services: Google or the AI model
        self.configure(api_key, api_url, provider, enabled)

    def configure(
        self,
        api_key: str,
        local_url: str = "",
        provider: str = "",
        enabled: bool = True,
    ) -> None:
        """`provider`: "local" (Whisper server at `local_url`), "api" (the key), "" = guess."""
        self.enabled = enabled
        self.api_key = api_key
        self.local_url = (local_url or "").rstrip("/")
        if provider not in ("local", "api"):
            provider = "local" if self.local_url else ("api" if api_key else "")
        self.provider = provider
        if provider == "local":
            self.api_url, self.speech_key = self.local_url or LOCAL_URL, ""
        elif api_key:
            self.api_url, self.speech_key = api_url_for(api_key), api_key
        else:
            self.api_url, self.speech_key = "", ""
        self.chat_url = api_url_for(api_key) if api_key else ""
        self._models = None
        self._chat_model: str | None | bool = False  # False = not looked up yet

    @property
    def available(self) -> bool:
        return self.enabled and bool(self.api_url) and self.transcoder.available

    @property
    def chat_available(self) -> bool:
        """The key's chat model can translate (independent of where speech is transcribed)."""
        return bool(self.chat_url)

    # -- models -------------------------------------------------------------------

    def _headers(self, key: str | None = None) -> dict:
        key = self.speech_key if key is None else key
        return {"Authorization": f"Bearer {key}"} if key else {}

    def list_models(self, key: str | None = None, url: str | None = None) -> list[str]:
        response = self.session.get(
            f"{url or self.api_url}/models", headers=self._headers(key), timeout=15
        )
        if response.status_code in (401, 403):
            raise TranscribeError("API key rejected", 400)
        response.raise_for_status()
        return [m.get("id", "") for m in response.json().get("data") or []]

    def check_key(self, key: str) -> bool:
        """Whether the key works and the account offers a speech-to-text model."""
        try:
            models = self.list_models(key, api_url_for(key))
        except TranscribeError:
            return False
        return pick_speech_model(models) is not None

    def check_local(self, url: str) -> bool:
        """Whether a Whisper server answers at `url` (whisper.cpp has no /models)."""
        try:
            response = self.session.get(f"{url.rstrip('/')}/models", timeout=8)
        except requests.RequestException:
            return False
        if (
            response.status_code == 404
        ):  # whisper.cpp: the server is there, no model list
            return True
        try:
            return response.ok and (
                pick_speech_model(
                    m.get("id", "") for m in response.json().get("data") or []
                )
                is not None
            )
        except ValueError:
            return False

    def models(self) -> tuple[str, str | None]:
        """(speech model, chat model or None)."""
        if self._models is None:
            try:
                offered = set(self.list_models())
            except requests.HTTPError as e:
                if self.provider != "local" or e.response.status_code != 404:
                    raise TranscribeError(f"speech API error: {e}") from None
                offered = {"whisper-1"}  # whisper.cpp: one loaded model, no list
            except (requests.RequestException, ValueError) as e:
                raise TranscribeError(f"speech server unreachable: {e}") from None
            speech = pick_speech_model(offered) or (
                "whisper-1" if self.provider == "local" else None
            )
            if not speech:
                raise TranscribeError("this account has no speech-to-text model", 501)
            self._models = (speech, self.chat_model())
        return self._models

    def chat_model(self) -> str | None:
        """Chat model of the key's account, for AI translation."""
        if self._chat_model is False:
            self._chat_model = None
            if self.chat_url:
                try:
                    offered = set(self.list_models(self.api_key, self.chat_url))
                    self._chat_model = next(
                        (m for m in CHAT_MODELS if m in offered), None
                    )
                except (requests.RequestException, ValueError, TranscribeError) as e:
                    logger.info("chat model lookup failed: %s", e)
                    self._chat_model = False  # try again next time
                    return None
        return self._chat_model

    # -- one window -----------------------------------------------------------------

    def window(
        self,
        stream_url: str,
        start: float,
        duration: float,
        audio_track: int = 0,
        translate_to: str = "",
        language: str = "",
        engine: str = "google",
        via: str = "auto",
    ) -> dict:
        """Subtitle lines for [start, start + duration) of the file, cached."""
        duration = max(5.0, min(float(duration), MAX_WINDOW))
        key = (
            stream_url,
            round(start, 1),
            round(duration, 1),
            audio_track,
            translate_to,
            language,
            engine,
            via,
        )
        while True:
            with self._lock:
                if key in self._cache:
                    self._cache.move_to_end(key)
                    return self._cache[key]
                waiting = self._inflight.get(key)
                if waiting is None:
                    self._inflight[key] = threading.Event()
                    break
            waiting.wait(
                120
            )  # the same window is already being made (two tabs, a retry)
        try:
            result = self._make(
                stream_url,
                start,
                duration,
                audio_track,
                translate_to,
                language,
                engine,
                via,
            )
            with self._lock:
                self._cache[key] = result
                while len(self._cache) > CACHE_SIZE:
                    self._cache.popitem(last=False)
            return result
        finally:
            with self._lock:
                self._inflight.pop(key).set()

    def _make(
        self, url, start, duration, audio_track, translate_to, language, engine, via
    ) -> dict:
        clip_start = max(0.0, start - OVERLAP)
        audio = self.extract_audio(
            url, clip_start, duration + (start - clip_start) + OVERLAP, audio_track
        )
        if (
            len(audio) < 2000
        ):  # nothing decodable in this window (end of file, bad data)
            return {"lines": [], "language": language or None, "translated": False}
        # Through English: Whisper translates the speech itself, Google does the rest.
        english_first = (
            translate_to
            and language
            and language not in ("en", translate_to)
            and (via == "english" or (via == "auto" and language in VIA_ENGLISH))
        )
        data = None
        if english_first:
            try:
                data = self.transcribe(audio, language, task="translate")
            except TranscribeError as e:  # e.g. a model without translation
                logger.info("Whisper translation failed, transcribing: %s", e)
        if data is None:
            english_first = False
            data = self.transcribe(audio, language)
        spoken = language or _language_code((data.get("language") or "").lower())
        detected = "en" if english_first else spoken
        lines = clean_segments(
            data.get("segments") or [],
            clip_start,
            start + duration + OVERLAP,
            data.get("words") or [],
        )
        lines = [line for line in lines if start <= line.start < start + duration]
        translated = False
        if translate_to and lines and detected != translate_to:
            try:
                texts = [line.text for line in lines]
                if self.translator is not None:
                    texts = self.translator.translate(texts, translate_to, engine)
                else:
                    texts = self.translate(texts, translate_to)
                for line, text in zip(lines, texts, strict=True):
                    line.text = text
                translated = True
            except Exception as e:  # keep the original text rather than nothing
                logger.info("translation failed: %s", e)
        if english_first and not translated and translate_to == "en":
            translated = True  # Whisper's English is the translation
        return {
            "lines": [line.to_dict() for line in lines],
            "language": spoken
            or None,  # what is spoken (the player sends it back as a hint)
            "translated": translated or english_first,
            "via": "english" if english_first else "direct",
        }

    def extract_audio(
        self, url: str, start: float, duration: float, audio_track: int = 0
    ) -> bytes:
        """The window's audio as 16 kHz mono FLAC (what Whisper works with internally).

        Written to a temporary file, not a pipe: only then can ffmpeg fill in the
        FLAC header (length), which whisper.cpp needs to read the audio at all.
        """
        if not self._extract_slots.acquire(timeout=60):
            raise TranscribeError("too many AI subtitle requests", 503, retry_after=5)
        fd, path = tempfile.mkstemp(suffix=".flac", prefix="sf-audio-")
        os.close(fd)
        try:
            subprocess.run(
                [
                    self.transcoder.ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-y",
                    "-ss",
                    f"{max(0.0, start):.3f}",
                    "-t",
                    f"{duration:.3f}",
                    "-i",
                    url,
                    "-map",
                    f"0:a:{audio_track}",
                    "-vn",
                    "-sn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "flac",
                    path,
                ],
                capture_output=True,
                timeout=90,
            )
            with open(path, "rb") as f:
                return f.read()
        except subprocess.TimeoutExpired:
            raise TranscribeError(
                "the torrent is too slow to read this part yet", 504, retry_after=10
            ) from None
        finally:
            self._extract_slots.release()
            with contextlib.suppress(OSError):
                os.remove(path)

    def transcribe(
        self, audio: bytes, language: str = "", task: str = "transcribe"
    ) -> dict:
        """Speech -> text with timestamps; task "translate" gives English text."""
        speech_model, _ = self.models()
        endpoint = "transcriptions"
        if task == "translate":
            if self.provider == "local":
                # whisper.cpp serves one path; a form field switches it to translation.
                pass
            else:
                endpoint = "translations"  # OpenAI-style APIs: English only
                speech_model = (
                    "whisper-large-v3" if "groq" in self.api_url else speech_model
                )
        fields = [
            ("model", speech_model),
            ("response_format", "verbose_json"),
            ("temperature", "0"),
            ("timestamp_granularities[]", "segment"),
            ("timestamp_granularities[]", "word"),  # exact line starts (Groq/OpenAI)
        ]
        if language:
            fields.append(("language", language))
        if task == "translate" and self.provider == "local":
            fields.append(("translate", "true"))
        try:
            response = self.session.post(
                f"{self.api_url}/audio/{endpoint}",
                headers=self._headers(),
                data=fields,
                files={"file": ("audio.flac", audio, "audio/flac")},
                timeout=90,
            )
        except requests.RequestException as e:
            raise TranscribeError(f"speech API unreachable: {e}") from None
        self._raise_for(response)
        return response.json()

    def translate(self, texts: list[str], target: str) -> list[str]:
        """Translate subtitle lines, one output line per input line."""
        chat_model = self.chat_model()
        if not chat_model:
            raise TranscribeError("no chat model for translation", 501)
        name = f"{LANGUAGE_NAMES.get(target, target)} ({target})"
        prompt = (
            f"Translate these film subtitle lines into {name}. Keep them short and natural, "
            "like real subtitles; keep names. Reply with JSON only: "
            '{"lines": [...]} with exactly one translated string per input line, same order.'
        )
        try:
            response = self.session.post(
                f"{self.chat_url}/chat/completions",
                headers=self._headers(self.api_key),
                json={
                    "model": chat_model,
                    "temperature": 0.2,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": prompt},
                        {
                            "role": "user",
                            "content": json.dumps({"lines": texts}, ensure_ascii=False),
                        },
                    ],
                },
                timeout=60,
            )
        except requests.RequestException as e:
            raise TranscribeError(f"translation unreachable: {e}") from None
        self._raise_for(response)
        try:
            content = response.json()["choices"][0]["message"]["content"]
            lines = json.loads(content)["lines"]
        except (KeyError, IndexError, TypeError, ValueError):
            raise TranscribeError("translation returned no lines") from None
        if not isinstance(lines, list) or len(lines) != len(texts):
            raise TranscribeError("translation returned a different number of lines")
        return [
            " ".join(str(line).split()) or original
            for line, original in zip(lines, texts, strict=True)
        ]

    @staticmethod
    def _raise_for(response: requests.Response) -> None:
        if response.ok:
            return
        retry = response.headers.get("retry-after")
        try:
            message = response.json().get("error", {}).get("message") or response.text
        except ValueError:
            message = response.text
        if response.status_code == 429:
            raise TranscribeError(
                "speech API limit reached, wait a moment",
                429,
                retry_after=float(retry) if retry else 20,
            )
        if response.status_code in (401, 403):
            raise TranscribeError("the AI subtitles API key was rejected", 401)
        raise TranscribeError(
            f"speech API error {response.status_code}: {message[:200]}"
        )


# Whisper's verbose_json reports the language by name ("english"), sometimes by code.
_NAME_TO_CODE = {name.lower(): code for code, name in LANGUAGE_NAMES.items()} | {
    "english": "en",
    "czech": "cs",
    "slovak": "sk",
    "german": "de",
    "french": "fr",
    "spanish": "es",
    "italian": "it",
    "polish": "pl",
    "russian": "ru",
    "japanese": "ja",
    "korean": "ko",
    "chinese": "zh",
}


def _language_code(value: str) -> str:
    value = value.strip().lower()
    if len(value) == 2:
        return value
    return _NAME_TO_CODE.get(value, value)
