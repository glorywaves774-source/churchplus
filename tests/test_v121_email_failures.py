"""1.2.1: when email isn't set up (or the mail server fails), pages keep working and say what happened."""
import re
import smtplib

import pytest

from app.models import Member, TenantUser
from app.services import mailer
from app.utils.tenancy import tenant_bypass, tenant_context
from tests.test_stage3_security import _role_id, _setup, add_person, login, user_by_email  # noqa: F401
from tests.test_stage8_engage import add_member, grace  # noqa: F401


@pytest.fixture
def no_email(app):
    """Like the live site before email settings are filled in: SMTP chosen, no server given."""
    app.config.update(MAIL_BACKEND="smtp", MAIL_SMTP_HOST=None)
    yield
    app.config.update(MAIL_BACKEND="console")


def flashes(client):
    with client.session_transaction() as s:
        return [m for _, m in s.get("_flashes", [])]


def test_send_raises_a_plain_mail_error_when_email_is_not_set_up(app, no_email):
    with app.test_request_context():
        with pytest.raises(mailer.MailError, match="isn't set up"):
            mailer.send("a@b.test", "Hi", "church_message", text="x", church=None)
        assert mailer.try_send("a@b.test", "Hi", "church_message", text="x", church=None) is False


def test_mail_server_refusal_becomes_mail_error(app, monkeypatch):
    app.config.update(MAIL_BACKEND="smtp", MAIL_SMTP_HOST="smtp.example.test")
    def refuse(_mail):
        raise smtplib.SMTPAuthenticationError(535, b"bad password")
    monkeypatch.setattr(mailer, "_send_smtp", refuse)
    try:
        with app.test_request_context():
            with pytest.raises(mailer.MailError, match="didn't accept"):
                mailer.send("a@b.test", "Hi", "church_message", text="x", church=None)
    finally:
        app.config.update(MAIL_BACKEND="console")


def test_inviting_a_team_member_without_email_still_works(client, db, two_churches, no_email):
    login(client, "owner@grace.test")
    role = _role_id(db, two_churches["grace"], "membership_officer")
    res = client.post("/team/invite", data={"first_name": "Yaa", "last_name": "Boakye", "email": "yaa@grace.test",
                                            "branch": "", "roles": [role]})
    assert res.status_code == 302
    msg = " ".join(flashes(client))
    assert "couldn't be sent" in msg and re.search(r"http://localhost/set-password/\S+", msg)
    assert "Yaa Boakye" in client.get("/team").get_data(as_text=True)


def test_resend_invitation_without_email_no_longer_crashes(client, db, two_churches, no_email):
    login(client, "owner@grace.test")
    acct = add_person(db, two_churches["grace"], "kofi@grace.test", "membership_officer")
    res = client.post(f"/team/{acct}/resend")
    assert res.status_code == 302                      # was an error page (500)
    msg = " ".join(flashes(client))
    # Kofi already has a password, so he is told to sign in rather than given a set-up link.
    assert "already have a ChurchPlus account" in msg and "/login" in msg and "set-password" not in msg


def test_member_portal_invite_without_email_no_longer_crashes(client, db, grace, no_email):
    mid = add_member(db, grace, "Ama", email="ama@example.test")
    with tenant_context(grace):
        pid = db.session.get(Member, mid).public_id
    res = client.post(f"/portal/invite/{pid}")
    assert res.status_code == 302
    assert "/set-password/" in " ".join(flashes(client))
    with tenant_context(grace):
        assert db.session.query(TenantUser).filter_by(member_id=mid).one().is_portal_only


def test_forgot_password_without_email_gives_the_usual_answer(client, two_churches, no_email):
    res = client.post("/forgot-password", data={"email": "owner@grace.test"}, follow_redirects=True)
    assert res.status_code == 200 and "If that email has a ChurchPlus account" in res.get_data(as_text=True)


def test_registration_without_email_still_creates_the_church(client, db, no_email):
    res = client.post("/register", data={
        "church_name": "Hope Chapel", "kind": "independent", "country": "GH", "city": "Ho", "plan": "starter",
        "first_name": "Esi", "last_name": "Agbeko", "email": "esi@hope.test", "password": "Strong-Pass-123",
        "confirm": "Strong-Pass-123", "accept": "y"})
    assert res.status_code == 302
    assert "couldn't send the email confirmation" in " ".join(flashes(client))
    assert user_by_email(db, "esi@hope.test")
    resend = client.post("/verify-email/resend")
    assert resend.status_code == 302 and "couldn't be sent" in " ".join(flashes(client))


def test_link_messages_stay_until_closed():
    from pathlib import Path
    js = (Path(__file__).resolve().parent.parent / "static" / "js" / "app.js").read_text()
    assert 'if (kind !== "danger" && kind !== "error" && !url) { setTimeout(dismiss, 5000); }' in js
    assert "Copy link" in js and "innerHTML = message" not in js
