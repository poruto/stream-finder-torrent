"""Gzip for text responses, so phones on Wi-Fi download a fraction of the bytes.

Pages, JSON, subtitles, CSS and JS shrink to a quarter or less. Streams, ranges,
small bodies and already compressed files are sent as they are. Static files are
compressed once per version and kept in memory.
"""

import gzip

from flask import Flask, Response, request

from .caching import LRU

LEVEL = 5  # nearly the size of level 9 at half the CPU (a Raspberry Pi)
STATIC_LEVEL = 9  # static files are compressed once per version: the smallest
MIN_SIZE = 1024  # below one network packet gzip saves nothing
COMPRESSIBLE = {
    "text/html",
    "text/css",
    "text/plain",
    "text/vtt",
    "text/javascript",
    "application/javascript",
    "application/json",
    "application/manifest+json",
    "image/svg+xml",
}

_static: LRU = LRU(64)  # (path, etag) -> gzipped body of a static file


def init_app(app: Flask) -> None:
    app.after_request(compress)


def compress(response: Response) -> Response:
    if (
        response.status_code != 200
        or response.mimetype not in COMPRESSIBLE
        or "Content-Encoding" in response.headers
        or "no-transform" in response.headers.get("Cache-Control", "")
    ):
        return response
    static = response.direct_passthrough and request.path.startswith("/static/")
    if response.is_streamed and not static:
        return response  # generators (live streams) must not be buffered
    length = response.content_length
    if length is not None and length < MIN_SIZE:
        return response
    response.vary.add("Accept-Encoding")
    if request.accept_encodings.quality("gzip") <= 0:
        return response

    etag, weak = response.get_etag()
    key = (request.path, etag)
    body = _static.get(key) if static and etag else None
    if body is None:
        response.direct_passthrough = False
        data = response.get_data()  # reads (and later closes) a static file
        if len(data) < MIN_SIZE:
            return response
        body = gzip.compress(data, STATIC_LEVEL if static else LEVEL, mtime=0)
        if static and etag:
            _static[key] = body
    elif hasattr(response.response, "close"):
        response.response.close()  # the static file, unread: its gzip is cached
    response.set_data(body)
    response.headers["Content-Encoding"] = "gzip"
    # Ranges of the gzip are not ranges of the file.
    response.headers.pop("Accept-Ranges", None)
    if etag and not weak:
        # Another body than the file: a weak validator, which still matches for 304s.
        response.set_etag(etag, weak=True)
    return response
