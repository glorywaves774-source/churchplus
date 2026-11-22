"""Shared test fixtures.

Tests run against in-memory SQLite by default. Set TEST_DATABASE_URL to a
PostgreSQL database to run the same suite against PostgreSQL.
"""
from __future__ import annotations

import pytest

from app import create_app
from app.cli import seed_plans
from app.config import normalise_database_url
from app.extensions import db as _db
from app.models import User
from app.services.provisioning import provision_church
from app.utils.tenancy import reset_tenant_state, tenant_bypass


@pytest.fixture()
def app(tmp_path, monkeypatch):
    import os
    url = os.environ.get("TEST_DATABASE_URL")
    overrides = {"LOCAL_STORAGE_PATH": str(tmp_path / "uploads")}
    if url:
        overrides["SQLALCHEMY_DATABASE_URI"] = normalise_database_url(url)
    application = create_app("testing", **overrides)

    # Tests keep one app context open, which Flask reuses for every test request.
    # Give each request a clean `g` and database session, as a real server does.
    def _fresh_request_state():
        from flask import g
        g.__dict__.clear()
    application.before_request_funcs.setdefault(None, []).insert(0, _fresh_request_state)

    @application.teardown_request
    def _fresh_session(exc):
        _db.session.remove()

    with application.app_context():
        _db.drop_all()
        _db.create_all()
        yield application
        _db.session.remove()
        _db.drop_all()
    reset_tenant_state()


@pytest.fixture()
def db(app):
    return _db


@pytest.fixture()
def client(app):
    return app.test_client()


def make_user(email: str, **kw) -> User:
    user = User(email=email, first_name=kw.get("first_name", "Test"), last_name=kw.get("last_name", "User"))
    user.set_password(kw.get("password", "Correct-Horse-9"))
    return user


@pytest.fixture()
def two_churches(db):
    """Two independent churches, each provisioned with defaults. Returns their ids."""
    with tenant_bypass():
        seed_plans()
        grace = provision_church(name="Grace Chapel International", owner=make_user("owner@grace.test"),
                                 plan_code="growth", city="Accra")
        word = provision_church(name="Living Word Assembly", owner=make_user("owner@word.test"),
                                plan_code="starter", city="Kumasi")
        db.session.commit()
        ids = {"grace": grace.id, "word": word.id}
    db.session.expunge_all()
    return ids
