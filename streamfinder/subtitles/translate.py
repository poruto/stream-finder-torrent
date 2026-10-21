"""Machine translation of subtitle lines.

Two engines, picked per request:
* "google": Google Translate's free web endpoint - no key, no account, good quality.
* "ai": the chat model of the AI subtitles account (Groq/OpenAI) - more natural
  dialogue, needs that key.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from .transcribe import TranscribeError

logger = logging.getLogger(__name__)

GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
GOOGLE_BATCH = 4000  # characters per request
AI_BATCH = 40  # lines per request
# Groq's free tier allows ~8000 tokens a minute, a whole film needs a few minutes of
# them: an AI translation waits out the limit this long, then Google does the rest.
AI_PATIENCE = 300


class TranslateError(Exception):
    pass


class Translator:
    def __init__(self, transcriber=None):
        self.transcriber = transcriber  # its chat model is the "ai" engine
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "Mozilla/5.0"
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="translate")

    def engines(self) -> list[str]:
        return ["google"] + (
            ["ai"] if self.transcriber and self.transcriber.chat_available else []
        )

    def translate(
        self,
        lines: list[str],
        target: str,
        engine: str = "google",
        patience: float = AI_PATIENCE,
    ) -> list[str]:
        """One translated line per input line (line breaks inside a line become spaces).

        `patience`: seconds the AI engine waits out its rate limit before Google steps in."""
        lines = [" ".join(line.split()) for line in lines]
        if not lines:
            return []
        if engine == "ai":
            if not self.transcriber or not self.transcriber.chat_available:
                raise TranslateError("AI translation needs a Groq or OpenAI key")
            batches = [lines[i : i + AI_BATCH] for i in range(0, len(lines), AI_BATCH)]
            deadline = time.monotonic() + patience
            work = lambda batch: self._ai(batch, target, deadline)
        else:
            batches = _batches(lines, GOOGLE_BATCH)
            work = lambda batch: self._google(batch, target)
        try:
            results = list(self._pool.map(work, batches))
        except TranslateError:
            raise
        except Exception as e:  # network errors, the AI engine's own errors
            raise TranslateError(str(e)) from None
        return [line for batch in results for line in batch]

    def _ai(self, lines: list[str], target: str, deadline: float) -> list[str]:
        """The chat model's translation, waiting out its rate limit until `deadline`;
        Google's when the model fails, so one bad batch doesn't lose the whole file."""
        retried = False
        while True:
            try:
                return self.transcriber.translate(lines, target)
            except TranscribeError as e:
                wait = e.retry_after if e.status == 429 else None
                if wait is not None and time.monotonic() + wait < deadline:
                    time.sleep(wait)
                    continue
                if e.status == 502 and not retried:
                    # The model now and then drops or merges a line: ask once more.
                    retried = True
                    continue
                logger.warning(
                    "AI translation failed (%s): Google Translate for %d lines",
                    e,
                    len(lines),
                )
                return self._google(lines, target)

    def _google(self, lines: list[str], target: str) -> list[str]:
        try:
            response = self.session.post(
                GOOGLE_URL,
                params={"client": "gtx", "sl": "auto", "tl": target, "dt": "t"},
                data={"q": "\n".join(lines)},
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as e:
            raise TranslateError(f"Google Translate: {e}") from None
        text = "".join(part[0] or "" for part in data[0] or [] if part)
        out = text.split("\n")
        if len(out) != len(lines):
            # Google merged or split a line: translate this batch line by line instead.
            if len(lines) == 1:
                return [text.replace("\n", " ").strip() or lines[0]]
            return [self._google([line], target)[0] for line in lines]
        return [o.strip() or original for o, original in zip(out, lines, strict=True)]


def _batches(lines: list[str], limit: int) -> list[list[str]]:
    batches, current, size = [], [], 0
    for line in lines:
        if current and size + len(line) + 1 > limit:
            batches.append(current)
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        batches.append(current)
    return batches
