"""Stage 3: authentication, account security, team management, roles and audit trail."""
import re

import pytest

from app.cli import seed_plans
from app.models import AuditLog, LoginEvent, Role, Tenant, TenantUser, User
from app.services import mailer, security
from app.utils.tenancy import tenant_bypass, tenant_context
from tests.conftest import make_user
from tests.test_design_system import assert_csp_clean

PW = "Correct-Horse-9"


def login(client, email, password=PW):
    return client.post("/login", data={"email": email, "password": password})


def link_from_last_mail(path_prefix: str) -> str:
    return re.search(rf"http://localhost({path_prefix}\S+)", mailer.outbox[-1].text).group(1)


def add_person(db, tenant_id, email, role_slug, **kw):
    with tenant_context(tenant_id):
        role = db.session.query(Role).filter_by(slug=role_slug).one()
        user = make_user(email, **kw)
        with tenant_bypass():
            db.session.add(user)
            db.session.flush()
        acct = TenantUser(user_id=user.id, roles=[role])
        db.session.add(acct)
        db.session.commit()
        return acct.public_id


def user_by_email(db, email):
    db.session.expire_all()
    with tenant_bypass():
        return db.session.query(User).filter_by(email=email).one()


@pytest.fixture(autouse=True)
def _setup(db):
    with tenant_bypass():
        seed_plans()
        db.session.commit()
    mailer.outbox.clear()


# --------------------------------------------------------------------------- #
# Password recovery and email verification
# --------------------------------------------------------------------------- #
def test_forgot_password_same_answer_for_unknown_email(client, two_churches):
    a = client.post("/forgot-password", data={"email": "nobody@nowhere.test"}, follow_redirects=True)
    assert "If that email has a ChurchPlus account" in a.get_data(as_text=True)
    assert mailer.outbox == []


def test_reset_password_flow_is_single_use_and_ends_sessions(app, client, db, two_churches):
    other = app.test_client()
    login(other, "owner@grace.test")
    assert other.get("/dashboard").status_code == 200

    client.post("/forgot-password", data={"email": "owner@grace.test"})
    link = link_from_last_mail("/reset-password/")
    res = client.post(link, data={"password": "Brand-New-Pass-1", "confirm": "Brand-New-Pass-1"})
    assert res.status_code == 302
    assert client.get("/dashboard").status_code == 200          # signed in on this device
    assert other.get("/dashboard").status_code == 302           # other device signed out
    client.post("/logout")
    assert "expired or has already been used" in client.get(link, follow_redirects=True).get_data(as_text=True)
    assert login(client, "owner@grace.test", "Brand-New-Pass-1").headers["Location"].endswith("/dashboard")


def test_registration_sends_verification_and_link_confirms(client, db):
    client.post("/register", data={
        "church_name": "Hope Chapel", "kind": "independent", "country": "GH", "city": "Ho", "plan": "starter",
        "first_name": "Esi", "last_name": "Agbeko", "email": "esi@hope.test", "password": "Strong-Pass-123",
        "confirm": "Strong-Pass-123", "accept": "y"})
    assert mailer.outbox[-1].to == "esi@hope.test"
    assert "Please confirm your email address" in client.get("/account").get_data(as_text=True)
    client.get(link_from_last_mail("/verify-email/"))
    assert user_by_email(db, "esi@hope.test").email_verified_at is not None
    assert "Please confirm your email address" not in client.get("/account").get_data(as_text=True)


# --------------------------------------------------------------------------- #
# Lockout and sign-in history
# --------------------------------------------------------------------------- #
def test_lockout_after_repeated_failures(app, client, db, two_churches):
    app.config["RATELIMIT_ENABLED"] = False
    for _ in range(security.LOCKOUT_FAILURES):
        login(client, "owner@grace.test", "wrong-password")
    html = client.post("/login", data={"email": "owner@grace.test", "password": PW}, follow_redirects=True).get_data(as_text=True)
    assert "Too many unsuccessful attempts" in html
    with tenant_bypass():
        events = [e.event for e in db.session.query(LoginEvent).order_by(LoginEvent.id)]
    assert events.count("failed") == security.LOCKOUT_FAILURES and events[-1] == "locked"


def test_sign_in_history_shown_in_account(client, two_churches):
    login(client, "owner@grace.test", "wrong-password")
    login(client, "owner@grace.test")
    html = client.get("/account").get_data(as_text=True)
    assert "Wrong password" in html and "Signed in" in html
    assert_csp_clean(html)


# --------------------------------------------------------------------------- #
# Two-step sign-in
# --------------------------------------------------------------------------- #
def test_totp_matches_rfc6238_vector():
    # RFC 6238 test secret "12345678901234567890" (base32), time 59 -> 94287082 (8 digits) -> 287082
    import base64
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert security.totp_now(secret, at=59) == "287082"
    assert security.verify_totp(secret, "287082", at=59)
    assert not security.verify_totp(secret, "000000", at=59)


def _enable_2fa(client):
    page = client.get("/account/two-step").get_data(as_text=True)
    assert "<svg" in page
    assert_csp_clean(page)
    with client.session_transaction() as s:
        secret = s["cp_totp_setup"]
    html = client.post("/account/two-step", data={"code": security.totp_now(secret)}).get_data(as_text=True)
    codes = re.findall(r"<li>([A-Z0-9]{4}-[A-Z0-9]{4})</li>", html)
    assert len(codes) == 8
    return secret, codes


def test_two_step_sign_in_full_flow(client, db, two_churches):
    login(client, "owner@grace.test")
    secret, codes = _enable_2fa(client)
    client.post("/logout")

    res = login(client, "owner@grace.test")
    assert res.headers["Location"].endswith("/login/verify")
    assert client.get("/dashboard").status_code == 302                  # not signed in yet
    html = client.post("/login/verify", data={"code": "000000"}).get_data(as_text=True)
    assert "That code is not correct" in html
    res = client.post("/login/verify", data={"code": security.totp_now(secret)})
    assert res.headers["Location"].endswith("/dashboard")
    client.post("/logout")

    login(client, "owner@grace.test")
    client.post("/login/verify", data={"code": codes[0]})               # recovery code
    assert client.get("/dashboard").status_code == 200
    assert len(user_by_email(db, "owner@grace.test").recovery_codes) == 7
    client.post("/logout")
    login(client, "owner@grace.test")
    client.post("/login/verify", data={"code": codes[0]})               # used already
    assert client.get("/dashboard").status_code == 302


def test_two_step_disable_requires_password(client, db, two_churches):
    login(client, "owner@grace.test")
    _enable_2fa(client)
    client.post("/account/two-step/disable", data={"password": "wrong"})
    assert user_by_email(db, "owner@grace.test").two_factor_enabled
    client.post("/account/two-step/disable", data={"password": PW})
    assert not user_by_email(db, "owner@grace.test").two_factor_enabled


def test_two_step_pending_expires(client, two_churches, db):
    login(client, "owner@grace.test")
    secret, _ = _enable_2fa(client)
    client.post("/logout")
    login(client, "owner@grace.test")
    with client.session_transaction() as s:
        s["cp_2fa_user"]["t"] -= 600
        s.modified = True
    res = client.post("/login/verify", data={"code": security.totp_now(secret)})
    assert res.headers["Location"].endswith("/login")


# --------------------------------------------------------------------------- #
# Passwords and sessions
# --------------------------------------------------------------------------- #
def test_change_password(app, client, db, two_churches):
    other = app.test_client()
    login(other, "owner@grace.test")
    login(client, "owner@grace.test")
    html = client.post("/account", data={"action": "password", "pw-current": "nope", "pw-password": "Another-Pass-77",
                                         "pw-confirm": "Another-Pass-77"}).get_data(as_text=True)
    assert "current password is not correct" in html
    client.post("/account", data={"action": "password", "pw-current": PW, "pw-password": "Another-Pass-77",
                                  "pw-confirm": "Another-Pass-77"})
    assert client.get("/dashboard").status_code == 200
    assert other.get("/dashboard").status_code == 302


def test_sign_out_other_devices_also_kills_remember_cookie(app, client, two_churches):
    other = app.test_client()
    other.post("/login", data={"email": "owner@grace.test", "password": PW, "remember": "y"})
    login(client, "owner@grace.test")
    client.post("/account/sign-out-others")
    assert client.get("/dashboard").status_code == 200
    with other.session_transaction() as s:      # simulate the browser session ending; cookie remains
        s.clear()
    assert other.get("/dashboard").status_code == 302


def test_update_profile(client, db, two_churches):
    login(client, "owner@grace.test")
    client.post("/account", data={"action": "profile", "p-first_name": "Kofi", "p-last_name": "Mensah", "p-phone": "0241112222"})
    u = user_by_email(db, "owner@grace.test")
    assert (u.first_name, u.phone) == ("Kofi", "0241112222")


# --------------------------------------------------------------------------- #
# Team management
# --------------------------------------------------------------------------- #
def _role_id(db, tenant_id, slug):
    with tenant_context(tenant_id):
        return db.session.query(Role).filter_by(slug=slug).one().public_id


def test_invite_new_person_end_to_end(app, client, db, two_churches):
    login(client, "owner@grace.test")
    role = _role_id(db, two_churches["grace"], "membership_officer")
    res = client.post("/team/invite", data={"first_name": "Yaa", "last_name": "Boakye", "email": "yaa@grace.test",
                                            "branch": "", "roles": [role]})
    assert res.status_code == 302
    assert "Yaa Boakye" in client.get("/team").get_data(as_text=True)
    link = link_from_last_mail("/set-password/")
    newcomer = app.test_client()
    newcomer.post(link, data={"password": "Yaa-Strong-Pass1", "confirm": "Yaa-Strong-Pass1"})
    html = newcomer.get("/dashboard").get_data(as_text=True)
    assert "Grace Chapel International" in html
    assert newcomer.get("/team").status_code == 403                 # membership officer cannot manage users


def test_invite_existing_user_from_other_church(client, db, two_churches):
    login(client, "owner@grace.test")
    role = _role_id(db, two_churches["grace"], "auditor")
    client.post("/team/invite", data={"first_name": "x", "last_name": "y", "email": "owner@word.test", "roles": [role]})
    assert "/login" in mailer.outbox[-1].text and "set-password" not in mailer.outbox[-1].text
    html = client.post("/team/invite", data={"first_name": "x", "last_name": "y", "email": "owner@word.test",
                                             "roles": [role]}).get_data(as_text=True)
    assert "already has an account in your church" in html


def test_cannot_grant_roles_beyond_own_access(client, db, two_churches):
    add_person(db, two_churches["grace"], "admin@grace.test", "church_admin")
    login(client, "admin@grace.test")
    for slug in ("senior_pastor", "church_owner", "finance_manager"):
        client.post("/team/invite", data={"first_name": "A", "last_name": "B", "email": f"{slug}@x.test",
                                          "roles": [_role_id(db, two_churches["grace"], slug)]})
    with tenant_bypass():
        assert db.session.query(User).filter(User.email.like("%@x.test")).count() == 0
    page = client.get("/team/invite").get_data(as_text=True)
    assert "needs more access than you have" in page


def test_cannot_create_role_with_permissions_you_lack(client, db, two_churches):
    add_person(db, two_churches["grace"], "admin@grace.test", "church_admin")
    login(client, "admin@grace.test")
    client.post("/roles/new", data={"name": "Sneaky", "perm": ["members.view", "counselling.view"]})
    with tenant_context(two_churches["grace"]):
        assert db.session.query(Role).filter_by(slug="sneaky").count() == 0
    client.post("/roles/new", data={"name": "Welcome Team", "perm": ["visitors.view", "visitors.create"]})
    with tenant_context(two_churches["grace"]):
        role = db.session.query(Role).filter_by(slug="welcome_team").one()
        assert {p.code for p in role.permissions} == {"visitors.view", "visitors.create"}


def test_editing_role_keeps_permissions_editor_lacks(client, db, two_churches):
    add_person(db, two_churches["grace"], "admin@grace.test", "church_admin")
    login(client, "admin@grace.test")
    rid = _role_id(db, two_churches["grace"], "finance_manager")
    client.post(f"/roles/{rid}", data={"name": "Finance Manager", "perm": ["finance.view"]})
    with tenant_context(two_churches["grace"]):
        perms = {p.code for p in db.session.query(Role).filter_by(slug="finance_manager").one().permissions}
    assert "finance.approve" in perms and "finance.view" in perms and "finance.edit" not in perms


def test_owner_role_locked_and_system_roles_not_deletable(client, db, two_churches):
    login(client, "owner@grace.test")
    owner_role = _role_id(db, two_churches["grace"], "church_owner")
    client.post(f"/roles/{owner_role}", data={"name": "Owner", "perm": []})
    client.post(f"/roles/{_role_id(db, two_churches['grace'], 'auditor')}/delete")
    with tenant_context(two_churches["grace"]):
        assert len(db.session.query(Role).filter_by(slug="church_owner").one().permissions) == 147
        assert db.session.query(Role).filter_by(slug="auditor").count() == 1


def test_custom_role_delete_only_when_unused(client, db, two_churches):
    login(client, "owner@grace.test")
    client.post("/roles/new", data={"name": "Choir Lead", "perm": ["events.view"]})
    rid = _role_id(db, two_churches["grace"], "choir_lead")
    acct = add_person(db, two_churches["grace"], "choir@grace.test", "member")
    client.post(f"/team/{acct}", data={"branch": "", "roles": [rid]})
    client.post(f"/roles/{rid}/delete")
    with tenant_context(two_churches["grace"]):
        assert db.session.query(Role).filter_by(slug="choir_lead").count() == 1
    client.post(f"/team/{acct}", data={"branch": "", "roles": [_role_id(db, two_churches['grace'], 'member')]})
    client.post(f"/roles/{rid}/delete")
    with tenant_context(two_churches["grace"]):
        assert db.session.query(Role).filter_by(slug="choir_lead").count() == 0


def test_cannot_change_self_or_owner(client, db, two_churches):
    add_person(db, two_churches["grace"], "admin@grace.test", "church_admin")
    login(client, "admin@grace.test")
    with tenant_context(two_churches["grace"]):
        accts = {a.user.email: a.public_id for a in db.session.query(TenantUser)}
    member = _role_id(db, two_churches["grace"], "member")
    client.post(f"/team/{accts['admin@grace.test']}", data={"roles": [member]})
    client.post(f"/team/{accts['owner@grace.test']}/suspend")
    client.post(f"/team/{accts['owner@grace.test']}/remove")
    client.post(f"/team/{accts['admin@grace.test']}/suspend")
    with tenant_context(two_churches["grace"]):
        rows = {a.user.email: a for a in db.session.query(TenantUser)}
        assert [r.slug for r in rows["admin@grace.test"].roles] == ["church_admin"]
        assert not rows["owner@grace.test"].is_suspended and not rows["admin@grace.test"].is_suspended


def test_suspend_reactivate_and_remove(app, client, db, two_churches):
    acct = add_person(db, two_churches["grace"], "usher@grace.test", "member")
    usher = app.test_client()
    login(usher, "usher@grace.test")
    assert usher.get("/dashboard").status_code == 200
    login(client, "owner@grace.test")
    client.post(f"/team/{acct}/suspend")
    assert usher.get("/dashboard").status_code == 302
    client.post(f"/team/{acct}/suspend")
    assert usher.get("/dashboard").status_code == 200
    client.post(f"/team/{acct}/remove")
    assert usher.get("/dashboard").status_code == 302
    with tenant_bypass():
        assert db.session.query(User).filter_by(email="usher@grace.test").count() == 1   # login kept


def test_user_limit_from_plan(client, db, two_churches):
    for i in range(4):                                   # Living Word: Starter allows 5 users
        add_person(db, two_churches["word"], f"p{i}@word.test", "membership_officer")
    login(client, "owner@word.test")
    res = client.get("/team/invite", follow_redirects=True)
    assert "Your plan allows 5 users" in res.get_data(as_text=True)


def test_team_pages_cannot_reach_other_church(client, db, two_churches):
    with tenant_context(two_churches["word"]):
        other_acct = db.session.query(TenantUser).one().public_id
        other_role = db.session.query(Role).filter_by(slug="auditor").one().public_id
    login(client, "owner@grace.test")
    assert client.get(f"/team/{other_acct}").status_code == 404
    assert client.post(f"/team/{other_acct}/suspend").status_code == 404
    assert client.post(f"/team/{other_acct}/remove").status_code == 404
    assert client.get(f"/roles/{other_role}").status_code == 404
    assert client.post(f"/roles/{other_role}/delete").status_code == 404
    mine = _role_id(db, two_churches["grace"], "member")
    acct = add_person(db, two_churches["grace"], "m@grace.test", "member")
    client.post(f"/team/{acct}", data={"roles": [mine, other_role]})    # foreign role id smuggled in
    with tenant_context(two_churches["grace"]):
        roles = db.session.query(TenantUser).filter_by(public_id=acct).one().roles
        assert [r.slug for r in roles] == ["member"]


def test_team_and_role_pages_render_clean(client, db, two_churches):
    login(client, "owner@grace.test")
    for path in ("/team", "/team/invite", "/roles", "/roles/new", "/account", "/audit",
                 f"/roles/{_role_id(db, two_churches['grace'], 'auditor')}"):
        res = client.get(path)
        assert res.status_code == 200, path
        assert_csp_clean(res.get_data(as_text=True))


# --------------------------------------------------------------------------- #
# Audit trail
# --------------------------------------------------------------------------- #
def test_audit_trail_view_filter_and_export(client, db, two_churches):
    login(client, "owner@grace.test")
    client.post("/branches/new", data={"name": "=HYPERLINK(\"evil\")", "code": "EVIL", "country": "GH"})
    html = client.get("/audit").get_data(as_text=True)
    assert "Branch added" in html and "Church workspace created" in html
    assert "Church workspace created" not in client.get("/audit?category=branches").get_data(as_text=True)
    csv = client.get("/audit/export.csv")
    assert csv.mimetype == "text/csv" and "attachment" in csv.headers["Content-Disposition"]
    body = csv.get_data(as_text=True)
    assert "Branch added: '=HYPERLINK" in body or "'=HYPERLINK" in body or "Branch added: =HYPERLINK" in body
    for line in body.splitlines()[1:]:
        assert not line.startswith(("=", "+", "@"))
    assert "Living Word" not in body


def test_audit_permissions(client, db, two_churches):
    add_person(db, two_churches["grace"], "audit@grace.test", "auditor")
    add_person(db, two_churches["grace"], "member@grace.test", "member")
    login(client, "audit@grace.test")
    assert client.get("/audit").status_code == 200
    assert client.get("/audit/export.csv").status_code == 200
    client.post("/logout")
    login(client, "member@grace.test")
    assert client.get("/audit").status_code == 403
