"""On-the-fly audio conversion for files the browser can't play with sound.

Browsers decode H.264/HEVC video but not AC3, E-AC3, DTS or TrueHD audio, and
Firefox/Safari can't open Matroska at all. For such files ffmpeg copies the
video stream untouched and converts only the audio to AAC, streaming fragmented
MP4 to the <video> element. Converting audio is cheap enough for a Raspberry Pi.

Seeking restarts ffmpeg at a keyframe (`keyframe_at`), so audio and video start
together and the player knows the exact offset of the stream.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

from . import config
from .caching import LRU, KeyedLocks

logger = logging.getLogger(__name__)

BROWSER_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac", "mp2"}
CHUNK = 64 * 1024
MAX_SESSIONS = 3  # concurrent conversions (one per playing browser tab)
SLOT_WAIT = 5  # seconds a new conversion waits for a free slot
# ffmpeg gives up when TorrServer sends nothing this long (microseconds), instead of
# hanging forever on a dead torrent with the browser long gone.
RW_TIMEOUT = ["-rw_timeout", "60000000"]
# No console window for every ffmpeg on Windows (when run as a service or by pythonw).
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
PROBE_CACHE = 64  # files whose probe result is kept
PROBE_RETRY = 60  # seconds before a failed probe is tried again

_STREAM_RE = re.compile(
    r"Stream #0:(\d+)(?:\[\w+\])?(?:\((\w+)\))?: (Video|Audio): (\w+)[^\n]*"
)
_DURATION_RE = re.compile(
    r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)(?:, start: (-?\d+(?:\.\d+)?))?"
)
_FPS_RE = re.compile(r"(\d+(?:\.\d+)?) fps")
_INPUT_RE = re.compile(r"Input #0, ([\w,]+), from")
_TIMEBASE_RE = re.compile(r"#tb 0: (\d+)/(\d+)")
_SILENCE_RE = re.compile(r"silence_(start|end): (-?\d+(?:\.\d+)?)")


@dataclass
class AudioTrack:
    index: int  # position among audio streams (for -map 0:a:N)
    codec: str
    language: str = ""
    channels: int | None = None
    title: str = ""

    @property
    def browser_ok(self) -> bool:
        return self.codec in BROWSER_AUDIO


@dataclass
class ProbeResult:
    duration: float | None
    container: str
    video_codec: str | None
    audio: list[AudioTrack] = field(default_factory=list)
    start_time: float = 0.0  # MKV/TS often don't start at 0
    fps: float | None = None

    @property
    def matroska(self) -> bool:
        return "matroska" in self.container

    def to_dict(self) -> dict:
        return {
            "duration": self.duration,
            "container": self.container,
            "video_codec": self.video_codec,
            "matroska": self.matroska,
            "fps": self.fps,
            "audio": [{**t.__dict__, "browser_ok": t.browser_ok} for t in self.audio],
            "audio_ok": bool(self.audio) and self.audio[0].browser_ok,
        }


def _bundled_ffmpeg() -> str | None:
    """ffmpeg shipped inside the imageio-ffmpeg wheel (Windows/macOS dev machines)."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # not installed, or no binary for this platform
        return None


class TranscoderBusy(RuntimeError):
    """Every conversion slot is taken."""


class Transcoder:
    def __init__(self, ffmpeg: str | None = None, ffprobe: str | None = None):
        self.ffmpeg = (
            ffmpeg or config.FFMPEG_PATH or shutil.which("ffmpeg") or _bundled_ffmpeg()
        )
        self.ffprobe = ffprobe or config.FFPROBE_PATH or shutil.which("ffprobe")
        self._slots = threading.BoundedSemaphore(MAX_SESSIONS)
        # url -> (valid until, ProbeResult, or the error of a failed probe)
        self._probes: LRU = LRU(PROBE_CACHE)
        self._probing = KeyedLocks()
        self._lock = threading.Lock()
        self._active: dict[str, subprocess.Popen] = {}  # url -> its running conversion

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg)

    # -- probing ----------------------------------------------------------------

    def probe(self, url: str, timeout: int = 20) -> ProbeResult:
        """Container, duration and codecs of a stream (cached per URL).

        The player asks for it several times at once (probe, keyframe, stream):
        only one ffprobe runs, the others wait for its result. A failure is
        remembered for a minute, so every seek doesn't wait for another timeout.
        """
        with self._probing.hold(url):
            cached = self._probes.get(url)
            if cached is None or cached[0] < time.monotonic():
                try:
                    result = (
                        self._ffprobe(url, timeout)
                        if self.ffprobe
                        else self._ffmpeg_probe(url, timeout)
                    )
                    cached = (float("inf"), result, None)
                except (
                    subprocess.SubprocessError,
                    OSError,
                    RuntimeError,
                    ValueError,
                ) as e:
                    cached = (time.monotonic() + PROBE_RETRY, None, e)
                self._probes[url] = cached
        _, result, error = cached
        if error is not None:
            raise RuntimeError(f"probe failed: {error}")
        return result

    def _ffprobe(self, url: str, timeout: int) -> ProbeResult:
        entries = (
            "format=duration,format_name,start_time"
            ":stream=codec_type,codec_name,channels,avg_frame_rate:stream_tags=language,title"
        )
        out = subprocess.run(
            [
                self.ffprobe,
                "-v",
                "error",
                *RW_TIMEOUT,
                "-show_entries",
                entries,
                "-of",
                "json",
                url,
            ],
            capture_output=True,
            timeout=timeout,
            check=True,
            creationflags=NO_WINDOW,
        ).stdout
        data = json.loads(out or b"{}")
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        audio = [
            AudioTrack(
                index=i,
                codec=s.get("codec_name", ""),
                channels=s.get("channels"),
                language=(s.get("tags") or {}).get("language", ""),
                title=(s.get("tags") or {}).get("title", ""),
            )
            for i, s in enumerate(s for s in streams if s.get("codec_type") == "audio")
        ]
        fmt = data.get("format", {})
        fps = None
        num, _, den = (video.get("avg_frame_rate") or "").partition("/")
        if num.isdigit() and den.isdigit() and int(den):
            fps = round(int(num) / int(den), 3)
        return ProbeResult(
            duration=float(fmt["duration"]) if fmt.get("duration") else None,
            container=fmt.get("format_name", ""),
            video_codec=video.get("codec_name"),
            audio=audio,
            start_time=float(fmt.get("start_time") or 0),
            fps=fps,
        )

    def _ffmpeg_probe(self, url: str, timeout: int) -> ProbeResult:
        """Fallback when only ffmpeg exists: parse the stream summary it prints."""
        result = subprocess.run(
            [self.ffmpeg, "-hide_banner", *RW_TIMEOUT, "-i", url],
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=NO_WINDOW,
        )
        text = result.stderr.decode("utf-8", "replace")
        duration, start_time = None, 0.0
        if match := _DURATION_RE.search(text):
            h, m, sec, start = match.groups()
            duration = int(h) * 3600 + int(m) * 60 + float(sec)
            start_time = float(start or 0)
        video_codec, audio = None, []
        for _, language, kind, codec in _STREAM_RE.findall(text):
            if kind == "Video" and video_codec is None:
                video_codec = codec
            elif kind == "Audio":
                audio.append(
                    AudioTrack(index=len(audio), codec=codec, language=language or "")
                )
        if not audio and video_codec is None:
            raise RuntimeError("ffmpeg could not read the stream")
        fps = _FPS_RE.search(text)
        return ProbeResult(
            duration=duration,
            container=(_INPUT_RE.search(text) or [None, ""])[1],
            video_codec=video_codec,
            audio=audio,
            start_time=start_time,
            fps=float(fps.group(1)) if fps else None,
        )

    def keyframe_at(self, url: str, position: float, timeout: int = 20) -> float:
        """Time of the keyframe a copy-seek to `position` really starts at.

        With the video stream copied, output can only begin on a keyframe at or
        before the requested time; knowing it keeps the clock and subtitles exact.
        """
        if position <= 0:
            return 0.0
        result = subprocess.run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{position:.3f}",
                *RW_TIMEOUT,
                "-i",
                url,
                "-map",
                "0:v:0",
                "-c",
                "copy",
                "-frames:v",
                "1",
                "-copyts",
                "-f",
                "framecrc",
                "-",
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=NO_WINDOW,
        )
        timebase, pts = None, None
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            if match := _TIMEBASE_RE.match(line):
                timebase = int(match.group(1)) / int(match.group(2))
            elif line.startswith("0,") and timebase:
                pts = int(line.split(",")[2])
                break
        if pts is None:
            return position
        # -copyts reports raw timestamps, while -ss counts from the container's start time.
        start_time = self.probe(url).start_time
        return max(0.0, round(pts * timebase - start_time, 3))

    def speech(
        self, url: str, start: float, duration: float = 120, timeout: int = 60
    ) -> list[tuple[float, float]]:
        """Intervals (absolute seconds) where the audio is not silent, i.e. mostly speech.

        Band-limited to the voice range so music and effects weigh less.
        """
        result = subprocess.run(
            [
                self.ffmpeg,
                "-hide_banner",
                "-nostdin",
                "-ss",
                f"{max(0.0, start):.3f}",
                "-t",
                f"{duration:.0f}",
                *RW_TIMEOUT,
                "-i",
                url,
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-af",
                "highpass=f=250,lowpass=f=3000,silencedetect=noise=-32dB:d=0.35",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            timeout=timeout,
            check=False,
            creationflags=NO_WINDOW,
        )
        events = _SILENCE_RE.findall(result.stderr.decode("utf-8", "replace"))
        intervals, cursor = [], 0.0
        for kind, value in events:
            t = float(value)
            if kind == "start" and t > cursor:
                intervals.append((cursor, t))
            elif kind == "end":
                cursor = t
        if cursor < duration:
            intervals.append((cursor, duration))
        return [(start + a, start + b) for a, b in intervals if b - a > 0.2]

    # -- streaming --------------------------------------------------------------

    def command(
        self,
        url: str,
        start: float = 0,
        audio_track: int = 0,
        copy_audio: bool = False,
        video_codec: str | None = None,
    ) -> list[str]:
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
        if start > 0:
            # Before -i: fast seek via HTTP range requests. -noaccurate_seek makes the audio start on
            # the same keyframe as the copied video; otherwise lips and sound drift apart.
            cmd += ["-noaccurate_seek", "-ss", f"{start:.3f}"]
        cmd += [
            *RW_TIMEOUT,
            "-i",
            url,
            "-map",
            "0:v:0",
            "-map",
            f"0:a:{audio_track}?",
            "-sn",
            "-c:v",
            "copy",
        ]
        if video_codec in ("hevc", "h265"):
            cmd += ["-tag:v", "hvc1"]  # lets Safari play HEVC in MP4
        cmd += (
            ["-c:a", "copy"]
            if copy_audio
            else ["-c:a", "aac", "-b:a", "192k", "-ac", "2"]
        )
        cmd += ["-avoid_negative_ts", "make_zero", "-max_muxing_queue_size", "1024"]
        cmd += [
            "-f",
            "mp4",
            "-movflags",
            "frag_keyframe+empty_moov+default_base_moof",
            "pipe:1",
        ]
        return cmd

    def stream(
        self,
        url: str,
        start: float = 0,
        audio_track: int = 0,
        copy_audio: bool = False,
        video_codec: str | None = None,
    ) -> "Conversion":
        """Start ffmpeg and return its fragmented MP4 output.

        A new stream of a file replaces the one already running for it (the player
        seeked), so a stalled old ffmpeg never holds a slot. Raises TranscoderBusy
        when every slot stays taken.
        """
        with self._lock:
            previous = self._active.get(url)
        if previous is not None:
            previous.kill()  # its Conversion sees the end of output and frees the slot
        if not self._slots.acquire(timeout=SLOT_WAIT):
            raise TranscoderBusy("Too many conversions running")
        try:
            process = subprocess.Popen(
                self.command(url, start, audio_track, copy_audio, video_codec),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                creationflags=NO_WINDOW,
            )
        except BaseException:
            self._slots.release()
            raise
        with self._lock:
            self._active[url] = process
        return Conversion(self, url, process)

    def _finished(self, url: str, process: subprocess.Popen) -> None:
        with self._lock:
            if self._active.get(url) is process:
                del self._active[url]
        self._slots.release()


class Conversion:
    """ffmpeg's output as a response body; ffmpeg is killed as soon as the browser disconnects.

    An iterator with close() rather than a generator: the WSGI server calls close()
    even when the body was never read, so the slot is always given back.
    """

    def __init__(self, transcoder: Transcoder, url: str, process: subprocess.Popen):
        self.transcoder, self.url, self.process = transcoder, url, process
        self._closed = False

    def __iter__(self) -> Iterator[bytes]:
        return self

    def __next__(self) -> bytes:
        chunk = b"" if self._closed else self.process.stdout.read(CHUNK)
        if not chunk:
            self.close()
            raise StopIteration
        return chunk

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.process.kill()
            self.process.wait()
            self.process.stdout.close()
        finally:
            self.transcoder._finished(self.url, self.process)
