"""Signed, expiring, single-use tokens for links sent by email
(invitations, password resets, email verification).

A token embeds a fingerprint of the user's current password hash (or email
verification state), so it stops working as soon as it has been used.
"""
from __future__ import annotations

import hashlib

from flask import current_app
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.extensions import db
from app.models import User
from app.utils.tenancy import tenant_bypass

PURPOSES = {"set_password": 60 * 60 * 72, "reset_password": 60 * 60, "verify_email": 60 * 60 * 72}


def _serializer(purpose: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt=f"churchplus.{purpose}")


def _fingerprint(user: User, purpose: str) -> str:
    if purpose == "verify_email":
        state = f"{user.email_verified_at or ''}|{user.email}"
    else:
        state = user.password_hash or "no-password"
    return hashlib.sha256(state.encode()).hexdigest()[:16]


def make(user: User, purpose: str) -> str:
    if purpose not in PURPOSES:
        raise ValueError(purpose)
    return _serializer(purpose).dumps({"u": user.public_id, "f": _fingerprint(user, purpose)})


def read(token: str, purpose: str) -> User | None:
    """Return the user for a valid token, or None if invalid, expired or already used."""
    try:
        data = _serializer(purpose).loads(token, max_age=PURPOSES[purpose])
    except (BadSignature, SignatureExpired):
        return None
    with tenant_bypass():
        user = db.session.query(User).filter_by(public_id=data.get("u")).one_or_none()
    if user is None or data.get("f") != _fingerprint(user, purpose):
        return None
    return user
