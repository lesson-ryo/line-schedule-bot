"""Shared teacher authentication for every administration page."""

from __future__ import annotations

import hashlib
import hmac
import os
import time

from flask import request


COOKIE_NAME = "teacher_admin"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30
_COOKIE_PURPOSE = b"teacher-admin-login-v1"


def master_admin_token() -> str:
    """Return the shared teacher password without caching environment values."""
    explicit = os.environ.get("MASTER_ADMIN_TOKEN", "").strip()
    if explicit:
        return explicit
    legacy = os.environ.get("ADMIN_TOKEN", "").strip()
    if legacy:
        return legacy

    # Existing deployments already have tenant-specific passwords. Reuse the
    # Kansai password as a safe migration default until MASTER_ADMIN_TOKEN is set.
    from tenant_config import TENANTS

    for name in ("kansai", "kanto"):
        token = TENANTS[name].admin_token.strip()
        if token:
            return token
    return ""


def password_ok(password: str) -> bool:
    expected = master_admin_token()
    return bool(expected) and hmac.compare_digest(str(password or ""), expected)


def _signature(expires_at: int) -> str:
    token = master_admin_token()
    if not token:
        return ""
    payload = _COOKIE_PURPOSE + b":" + str(expires_at).encode("ascii")
    return hmac.new(token.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def teacher_session_ok() -> bool:
    raw = request.cookies.get(COOKIE_NAME, "")
    try:
        expires_text, actual = raw.split(".", 1)
        expires_at = int(expires_text)
    except (TypeError, ValueError):
        return False
    if expires_at < int(time.time()):
        return False
    expected = _signature(expires_at)
    return bool(expected) and hmac.compare_digest(actual, expected)


def set_teacher_cookie(response):
    expires_at = int(time.time()) + COOKIE_MAX_AGE
    value = f"{expires_at}.{_signature(expires_at)}"
    response.set_cookie(
        COOKIE_NAME,
        value,
        max_age=COOKIE_MAX_AGE,
        secure=True,
        httponly=True,
        samesite="Strict",
        path="/",
    )
    return response


def clear_teacher_cookie(response):
    response.delete_cookie(
        COOKIE_NAME,
        secure=True,
        httponly=True,
        samesite="Strict",
        path="/",
    )
    return response
