"""Account security: two-step sign-in (TOTP), recovery codes, sign-in history.

TOTP follows RFC 6238 (30-second steps, 6 digits, SHA-1), which every
authenticator app supports: Google Authenticator, Microsoft Authenticator,
Authy, 1Password, etc.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from datetime import timedelta
from io import BytesIO
from urllib.parse import quote

from flask import has_request_context, request
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models import LoginEvent, User
from app.models.base import utcnow

# --------------------------------------------------------------------------- #
# TOTP
# --------------------------------------------------------------------------- #
def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _hotp(secret: str, counter: int) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def totp_now(secret: str, at: float | None = None) -> str:
    return _hotp(secret, int((at or time.time()) // 30))


def verify_totp(secret: str, code: str, window: int = 1, at: float | None = None) -> bool:
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if not secret or len(code) != 6:
        return False
    step = int((at or time.time()) // 30)
    return any(hmac.compare_digest(_hotp(secret, step + d), code) for d in range(-window, window + 1))


def provisioning_uri(user: User, secret: str, issuer: str = "ChurchPlus") -> str:
    label = quote(f"{issuer}:{user.email}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"


def qr_svg(data: str) -> str:
    """QR code as inline SVG markup (no image files, no external services)."""
    import qrcode
    import qrcode.image.svg

    img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode()
    return svg[svg.index("<svg"):]


# --------------------------------------------------------------------------- #
# Recovery codes
# --------------------------------------------------------------------------- #
def new_recovery_codes(n: int = 8) -> list[str]:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return ["-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(2)) for _ in range(n)]


def hash_codes(codes: list[str]) -> list[str]:
    return [generate_password_hash(c.replace("-", "").upper(), method="scrypt") for c in codes]


def use_recovery_code(user: User, code: str) -> bool:
    cleaned = (code or "").replace("-", "").replace(" ", "").upper()
    remaining = list(user.recovery_codes or [])
    for i, h in enumerate(remaining):
        if check_password_hash(h, cleaned):
            remaining.pop(i)
            user.recovery_codes = remaining          # single use
            return True
    return False


def enable_two_factor(user: User, secret: str) -> list[str]:
    codes = new_recovery_codes()
    user.totp_secret = secret
    user.two_factor_enabled = True
    user.recovery_codes = hash_codes(codes)
    return codes


def disable_two_factor(user: User) -> None:
    user.totp_secret = None
    user.two_factor_enabled = False
    user.recovery_codes = None


# --------------------------------------------------------------------------- #
# Sign-in history and lockout
# --------------------------------------------------------------------------- #
LOCKOUT_FAILURES = 8
LOCKOUT_WINDOW = timedelta(minutes=15)


def _client() -> tuple[str | None, str | None]:
    if not has_request_context():
        return None, None
    ip = (request.headers.get("X-Forwarded-For", request.remote_addr) or "").split(",")[0].strip()[:45]
    return ip or None, (request.user_agent.string or "")[:255] or None


def record_login(event: str, user: User | None = None, email: str | None = None) -> LoginEvent:
    ip, agent = _client()
    ev = LoginEvent(user_id=getattr(user, "id", None), email=(email or getattr(user, "email", None)),
                    event=event, ip_address=ip, user_agent=agent)
    db.session.add(ev)
    return ev


def is_locked(user: User) -> bool:
    """Too many failed attempts recently: refuse further password tries for a while."""
    since = utcnow() - LOCKOUT_WINDOW
    failures = (db.session.query(LoginEvent)
                .filter(LoginEvent.user_id == user.id, LoginEvent.created_at >= since,
                        LoginEvent.event.in_(("failed", "two_factor_failed"))).count())
    if failures < LOCKOUT_FAILURES:
        return False
    last_success = (db.session.query(LoginEvent.created_at)
                    .filter(LoginEvent.user_id == user.id, LoginEvent.event == "success",
                            LoginEvent.created_at >= since).first())
    return last_success is None


def recent_logins(user: User, limit: int = 15) -> list[LoginEvent]:
    return (db.session.query(LoginEvent).filter_by(user_id=user.id)
            .order_by(LoginEvent.created_at.desc()).limit(limit).all())


def describe_agent(agent: str | None) -> str:
    a = (agent or "").lower()
    device = ("iPhone" if "iphone" in a else "iPad" if "ipad" in a else "Android" if "android" in a
              else "Mac" if "mac os" in a else "Windows" if "windows" in a else "Linux" if "linux" in a else "Unknown device")
    browser = ("Edge" if "edg/" in a else "Chrome" if "chrome" in a else "Firefox" if "firefox" in a
               else "Safari" if "safari" in a else "Browser")
    return f"{browser} on {device}"
