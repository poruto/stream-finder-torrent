"""Admin login for server-wide settings (API keys).

There is one admin account. Until it is changed it is admin / admin, and the
Settings page asks to change it. Only a password hash is stored.
"""

import hmac
import threading
import time
from functools import wraps

from flask import jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from . import config
from .i18n import t

DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin"
MAX_FAILURES = 5
LOCKOUT = 60  # seconds

_failures: dict[
    str, tuple[int, float]
] = {}  # client ip -> (failed attempts, locked until)
_lock = threading.Lock()


def using_default() -> bool:
    return not config.ADMIN_PASSWORD_HASH


def username() -> str:
    return config.ADMIN_USERNAME or DEFAULT_USERNAME


def is_admin() -> bool:
    return session.get("admin") is True


def locked_for(ip: str) -> int:
    """Seconds this client still has to wait after too many wrong passwords."""
    with _lock:
        count, until = _failures.get(ip, (0, 0.0))
    return max(0, int(until - time.time())) if count >= MAX_FAILURES else 0


def _record(ip: str, ok: bool) -> None:
    with _lock:
        if ok:
            _failures.pop(ip, None)
            return
        count, _ = _failures.get(ip, (0, 0.0))
        count += 1
        _failures[ip] = (count, time.time() + LOCKOUT if count >= MAX_FAILURES else 0.0)


def verify(name: str, password: str) -> bool:
    if using_default():
        ok = hmac.compare_digest(name, DEFAULT_USERNAME) and hmac.compare_digest(
            password, DEFAULT_PASSWORD
        )
    else:
        ok = hmac.compare_digest(name, username()) and check_password_hash(
            config.ADMIN_PASSWORD_HASH, password
        )
    return ok


def login(name: str, password: str) -> tuple[bool, str | None]:
    ip = request.remote_addr or "?"
    if wait := locked_for(ip):
        return False, t("login_locked", n=wait)
    ok = verify(name, password)
    _record(ip, ok)
    if not ok:
        time.sleep(0.5)  # slows down guessing
        return False, t("login_wrong")
    session.clear()
    session["admin"] = True
    session.permanent = True
    return True, None


def logout() -> None:
    session.pop("admin", None)


def change_credentials(new_username: str, new_password: str) -> None:
    config.save_settings(
        {
            "ADMIN_USERNAME": new_username,
            "ADMIN_PASSWORD_HASH": generate_password_hash(new_password),
        }
    )


def admin_required(view):
    """API decorator: 401 unless the admin is logged in."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_admin():
            return jsonify(success=False, error=t("admin_required")), 401
        return view(*args, **kwargs)

    return wrapper
