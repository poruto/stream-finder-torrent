"""Requests that change something must come from the app's own pages.

A page on another site could otherwise make the visitor's browser POST to this app
(the admin's session cookie goes along with a form). Browsers send Origin with every
POST and fetch (sendBeacon too), Referer with most: when either names another host
or port than the one the request was sent to, it is refused. Requests with neither
(curl, scripts, the Windows tray) are not from a browser page and pass.
"""

from urllib.parse import urlsplit

from flask import Flask, jsonify, request

from .i18n import t

CHECKED = {"POST", "PUT", "PATCH", "DELETE"}
DEFAULT_PORTS = {"http": 80, "https": 443}


def _address(netloc: str, scheme: str) -> tuple[str, int | None]:
    """(host, port) with the scheme's port filled in: "example.org" == "example.org:80"."""
    try:
        parts = urlsplit(f"//{netloc}")
        return (parts.hostname or "").lower(), parts.port or DEFAULT_PORTS.get(scheme)
    except ValueError:  # a malformed port
        return "", None


def same_origin() -> bool:
    """True unless Origin (or, without it, Referer) points to another host:port."""
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return True
    if source == "null":  # sandboxed frames, local files: never the app's own page
        return False
    parts = urlsplit(source)
    if parts.scheme not in DEFAULT_PORTS or not parts.netloc:
        return False
    sender = _address(parts.netloc, parts.scheme)
    # Behind a reverse proxy the browser's host is in X-Forwarded-Host (a page on
    # another site can't set that header without the browser asking us first).
    hosts = [request.host, *request.headers.get("X-Forwarded-Host", "").split(",")]
    return any(
        host.strip() and _address(host.strip(), parts.scheme) == sender
        for host in hosts
    )


def init_app(app: Flask) -> None:
    @app.before_request
    def check_origin():
        if request.method in CHECKED and not same_origin():
            return jsonify(success=False, error=t("err_cross_site")), 403
        return None
