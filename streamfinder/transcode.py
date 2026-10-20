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
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field

from . import config

logger = logging.getLogger(__name__)

BROWSER_AUDIO = {"aac", "mp3", "opus", "vorbis", "flac", "mp2"}
CHUNK = 64 * 1024
MAX_SESSIONS = 3  # concurrent conversions (one per playing browser tab)

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


class Transcoder:
    def __init__(self, ffmpeg: str | None = None, ffprobe: str | None = None):
        self.ffmpeg = ffmpeg or config.FFMPEG_PATH or shutil.which("ffmpeg")
        self.ffprobe = ffprobe or config.FFPROBE_PATH or shutil.which("ffprobe")
        self._slots = threading.BoundedSemaphore(MAX_SESSIONS)
        self._probes: dict[str, ProbeResult] = {}

    @property
    def available(self) -> bool:
        return bool(self.ffmpeg)

    # -- probing ----------------------------------------------------------------

    def probe(self, url: str, timeout: int = 20) -> ProbeResult:
        """Container, duration and codecs of a stream (cached per URL)."""
        if url not in self._probes:
            self._probes[url] = (
                self._ffprobe(url, timeout)
                if self.ffprobe
                else self._ffmpeg_probe(url, timeout)
            )
        return self._probes[url]

    def _ffprobe(self, url: str, timeout: int) -> ProbeResult:
        entries = (
            "format=duration,format_name,start_time"
            ":stream=codec_type,codec_name,channels,avg_frame_rate:stream_tags=language,title"
        )
        out = subprocess.run(
            [self.ffprobe, "-v", "error", "-show_entries", entries, "-of", "json", url],
            capture_output=True,
            timeout=timeout,
            check=True,
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
            [self.ffmpeg, "-hide_banner", "-i", url],
            capture_output=True,
            timeout=timeout,
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
    ) -> Iterator[bytes]:
        """Yield fragmented MP4; ffmpeg is killed as soon as the browser disconnects."""
        if not self._slots.acquire(timeout=5):
            raise RuntimeError("Too many conversions running")
        process = subprocess.Popen(
            self.command(url, start, audio_track, copy_audio, video_codec),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        try:
            while chunk := process.stdout.read(CHUNK):
                yield chunk
        finally:
            process.kill()
            process.wait()
            self._slots.release()
