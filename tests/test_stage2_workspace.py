"""Stage 2: registration, sign-in, church workspace, branches, denominations,
and cross-church security over real HTTP requests."""
import io
import re

import pytest

from app.cli import seed_plans
from app.models import (
    Branch, Role, Tenant, TenantKind, TenantStatus, TenantUser, User,
)
from app.services import mailer
from app.utils.context import SESSION_TENANT_KEY
from app.utils.tenancy import tenant_bypass, tenant_context
from tests.conftest import make_user

PW = "Correct-Horse-9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def login(client, email, password=PW, follow=False):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=follow)


def register(client, **over):
    data = {"church_name": "Bethel Prayer Ministry", "kind": "independent", "country": "GH", "city": "Accra",
            "church_phone": "0302000000", "plan": "growth", "first_name": "Akua", "last_name": "Asante",
            "email": "akua@bethel.test", "password": "Strong-Pass-123", "confirm": "Strong-Pass-123", "accept": "y"}
    data.update(over)
    return client.post("/register", data=data)


def branch_of(db, tenant_id):
    """Plain snapshot of a church's first branch (safe to read outside any church context)."""
    from types import SimpleNamespace
    db.session.expire_all()
    with tenant_bypass():
        b = db.session.query(Branch).filter_by(tenant_id=tenant_id).first()
        return SimpleNamespace(public_id=b.public_id, name=b.name, id=b.id)


@pytest.fixture(autouse=True)
def _plans_and_outbox(db):
    with tenant_bypass():
        seed_plans()
        db.session.commit()
    mailer.outbox.clear()


# --------------------------------------------------------------------------- #
# Registration and sign-in
# --------------------------------------------------------------------------- #
def test_register_creates_workspace_and_signs_in(client, db):
    res = register(client)
    assert res.status_code == 302 and res.headers["Location"].endswith("/dashboard")
    page = client.get("/dashboard").get_data(as_text=True)
    assert "Bethel Prayer Ministry" in page and "Getting started" in page and "Free trial" in page
    with tenant_bypass():
        t = db.session.query(Tenant).filter_by(name="Bethel Prayer Ministry").one()
        assert t.currency == "GHS" and t.timezone == "Africa/Accra" and t.status == TenantStatus.TRIAL
        assert t.city == "Accra"


def test_register_currency_follows_country(client, db):
    register(client, country="NG", email="tunde@lagos.test", church_name="Lagos Revival Centre")
    with tenant_bypass():
        t = db.session.query(Tenant).filter_by(name="Lagos Revival Centre").one()
    assert (t.currency, t.timezone) == ("NGN", "Africa/Lagos")


def test_register_validation(client, db):
    html = register(client, password="weakpass", confirm="weakpass").get_data(as_text=True)
    assert "Use at least 10 characters" in html
    html = register(client, confirm="Different-123").get_data(as_text=True)
    assert "Passwords do not match" in html
    html = register(client, accept="").get_data(as_text=True)
    assert "Please confirm to continue" in html


def test_register_duplicate_email(client, db, two_churches):
    html = register(client, email="owner@grace.test").get_data(as_text=True)
    assert "already exists" in html


def test_register_requires_approval_when_configured(app, client, db):
    app.config["REGISTRATION_REQUIRES_APPROVAL"] = True
    register(client)
    res = client.get("/dashboard")
    assert res.status_code == 302 and "/church/pending" in res.headers["Location"]
    assert "Awaiting approval" in client.get("/church/pending").get_data(as_text=True)


def test_login_logout(client, two_churches):
    res = login(client, "owner@grace.test")
    assert res.status_code == 302 and res.headers["Location"].endswith("/dashboard")
    assert "Grace Chapel International" in client.get("/dashboard").get_data(as_text=True)
    client.post("/logout")
    assert client.get("/dashboard").status_code == 302


def test_login_wrong_password_is_generic(client, two_churches):
    a = login(client, "owner@grace.test", "wrong-password").get_data(as_text=True)
    b = login(client, "nobody@nowhere.test", "wrong-password").get_data(as_text=True)
    assert "The email or password is not correct." in a and "The email or password is not correct." in b


def test_login_is_case_insensitive(client, two_churches):
    assert login(client, "OWNER@Grace.test").headers["Location"].endswith("/dashboard")


def test_anonymous_redirected_and_returned(client, two_churches):
    res = client.get("/branches?status=active")
    assert res.status_code == 302 and res.headers["Location"].endswith("/login")
    res = login(client, "owner@grace.test")
    assert res.headers["Location"] == "/branches?status=active"


def test_open_redirect_blocked(client, two_churches):
    with client.session_transaction() as s:
        s["cp_next"] = "//evil.example/steal"
    assert login(client, "owner@grace.test").headers["Location"].endswith("/dashboard")


def test_logout_requires_post(client, two_churches):
    login(client, "owner@grace.test")
    assert client.get("/logout").status_code == 405


def test_suspended_user_signed_out(client, db, two_churches):
    login(client, "owner@grace.test")
    with tenant_bypass():
        db.session.query(User).filter_by(email="owner@grace.test").one().is_active_account = False
        db.session.commit()
    assert client.get("/dashboard").status_code == 302
    html = login(client, "owner@grace.test", follow=True).get_data(as_text=True)
    assert "suspended" in html


def test_suspended_church_account_cannot_enter(client, db, two_churches):
    with tenant_context(two_churches["grace"]):
        db.session.query(TenantUser).one().is_suspended = True
        db.session.commit()
    res = login(client, "owner@grace.test")
    assert res.headers["Location"].endswith("/churches")


def test_suspended_church_workspace(client, db, two_churches):
    with tenant_bypass():
        db.session.get(Tenant, two_churches["grace"]).status = TenantStatus.SUSPENDED
        db.session.commit()
    login(client, "owner@grace.test")
    res = client.get("/dashboard")
    assert "/church/unavailable" in res.headers["Location"]


# --------------------------------------------------------------------------- #
# Cross-church security over HTTP
# --------------------------------------------------------------------------- #
def test_cannot_open_or_change_another_churchs_branch(client, db, two_churches):
    other = branch_of(db, two_churches["word"])
    login(client, "owner@grace.test")
    assert client.get(f"/branches/{other.public_id}/edit").status_code == 404
    assert client.post(f"/branches/{other.public_id}/edit", data={"name": "X", "code": "X", "country": "GH"}).status_code == 404
    assert client.post(f"/branches/{other.public_id}/status").status_code == 404
    assert client.post(f"/branches/{other.public_id}/delete").status_code == 404
    assert branch_of(db, two_churches["word"]).name == "Living Word Assembly - Headquarters"
    assert "Living Word" not in client.get("/branches").get_data(as_text=True)


def test_tampered_session_cannot_select_another_church(client, db, two_churches):
    login(client, "owner@grace.test")
    with client.session_transaction() as s:
        s[SESSION_TENANT_KEY] = two_churches["word"]
    res = client.get("/dashboard")
    assert res.status_code == 302 and res.headers["Location"].endswith("/churches")
    with client.session_transaction() as s:
        assert SESSION_TENANT_KEY not in s


def test_cannot_switch_to_church_without_account(client, db, two_churches):
    with tenant_bypass():
        word_pid = db.session.get(Tenant, two_churches["word"]).public_id
    login(client, "owner@grace.test")
    client.post(f"/churches/{word_pid}")
    assert "Grace Chapel International" in client.get("/dashboard").get_data(as_text=True)


def test_user_with_two_churches_can_switch(client, db, two_churches):
    with tenant_bypass():
        user = db.session.query(User).filter_by(email="owner@grace.test").one()
        word_pid = db.session.get(Tenant, two_churches["word"]).public_id
    with tenant_context(two_churches["word"]):
        role = db.session.query(Role).filter_by(slug="church_admin").one()
        db.session.add(TenantUser(user_id=user.id, roles=[role]))
        db.session.commit()
    res = login(client, "owner@grace.test")
    assert res.headers["Location"].endswith("/churches")
    page = client.get("/churches").get_data(as_text=True)
    assert "Grace Chapel International" in page and "Living Word Assembly" in page
    client.post(f"/churches/{word_pid}")
    dash = client.get("/dashboard").get_data(as_text=True)
    assert "Living Word Assembly" in dash and "Switch church" in dash


def test_member_role_gets_403_on_admin_pages(client, db, two_churches):
    with tenant_context(two_churches["grace"]):
        member_role = db.session.query(Role).filter_by(slug="member").one()
        user = make_user("member@grace.test")
        with tenant_bypass():
            db.session.add(user)
            db.session.flush()
        db.session.add(TenantUser(user_id=user.id, roles=[member_role]))
        db.session.commit()
    login(client, "member@grace.test")
    assert client.get("/dashboard").status_code == 200
    for path in ("/church/settings", "/branches", "/branches/new"):
        assert client.get(path).status_code == 403, path
    html = client.get("/dashboard").get_data(as_text=True)
    assert 'href="/branches"' not in html and 'href="/church/settings"' not in html


# --------------------------------------------------------------------------- #
# Church profile and logo
# --------------------------------------------------------------------------- #
def test_update_church_profile(client, db, two_churches):
    login(client, "owner@grace.test")
    res = client.post("/church/settings", data={
        "name": "Grace Chapel Int'l", "tagline": "Raising kingdom builders", "phone": "0302123456",
        "country": "GH", "currency": "GHS", "timezone": "Africa/Accra", "city": "Accra",
        "digital_address": "GA-183-8164", "primary_color": "#5b3fd1", "website": "gracechapel.org"})
    assert res.status_code == 302
    with tenant_bypass():
        t = db.session.get(Tenant, two_churches["grace"])
        assert t.name == "Grace Chapel Int'l" and t.website == "https://gracechapel.org"
    with tenant_context(two_churches["grace"]):
        from app.models import AuditLog
        assert db.session.query(AuditLog).filter_by(action="church.profile_updated").count() == 1


def test_profile_validation(client, two_churches):
    login(client, "owner@grace.test")
    html = client.post("/church/settings", data={"name": "Grace", "country": "GH", "currency": "GHS",
                                                 "timezone": "Africa/Accra", "primary_color": "purple",
                                                 "digital_address": "nowhere"}).get_data(as_text=True)
    assert "Use a colour like" in html and "GA-123-4567" in html


def test_logo_upload_serve_and_remove(client, db, two_churches):
    login(client, "owner@grace.test")
    res = client.post("/church/logo", data={"logo": (io.BytesIO(PNG), "logo.png")},
                      content_type="multipart/form-data")
    assert res.status_code == 302
    with tenant_bypass():
        key = db.session.get(Tenant, two_churches["grace"]).logo_key
    assert key.startswith(f"tenants/{two_churches['grace']}/branding/")
    logo = client.get("/church/logo")
    assert logo.status_code == 200 and logo.data == PNG and "private" in logo.headers["Cache-Control"]
    client.post("/church/logo/remove")
    assert client.get("/church/logo").status_code == 404


def test_logo_rejects_fake_image(client, two_churches):
    login(client, "owner@grace.test")
    res = client.post("/church/logo", data={"logo": (io.BytesIO(b"MZ\x90\x00 not an image"), "logo.png")},
                      content_type="multipart/form-data", follow_redirects=True)
    assert "do not match its type" in res.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# Branches
# --------------------------------------------------------------------------- #
def test_branch_crud(client, db, two_churches):
    login(client, "owner@grace.test")          # Grace is on Growth: 3 branches
    res = client.post("/branches/new", data={"name": "Tema Branch", "code": "tema", "city": "Tema", "country": "GH"})
    assert res.status_code == 302
    with tenant_context(two_churches["grace"]):
        b = db.session.query(Branch).filter_by(code="TEMA").one()
        b_id = b.public_id
    assert "Tema Branch" in client.get("/branches").get_data(as_text=True)
    assert "Tema Branch" in client.get("/branches?q=tem").get_data(as_text=True)
    assert "Tema Branch" not in client.get("/branches?status=inactive").get_data(as_text=True)

    html = client.post("/branches/new", data={"name": "Dup", "code": "TEMA", "country": "GH"}).get_data(as_text=True)
    assert "Another branch already uses this code" in html

    client.post(f"/branches/{b_id}/edit", data={"name": "Tema Community 25", "code": "TEMA", "country": "GH"})
    client.post(f"/branches/{b_id}/status")
    db.session.expire_all()
    with tenant_context(two_churches["grace"]):
        b2 = db.session.query(Branch).filter_by(code="TEMA").one()
        assert b2.name == "Tema Community 25" and b2.is_active is False
    client.post(f"/branches/{b_id}/delete")
    with tenant_context(two_churches["grace"]):
        assert db.session.query(Branch).filter_by(code="TEMA").count() == 0


def test_headquarters_branch_protected(client, db, two_churches):
    hq = branch_of(db, two_churches["grace"])
    login(client, "owner@grace.test")
    client.post(f"/branches/{hq.public_id}/status")
    client.post(f"/branches/{hq.public_id}/delete")
    with tenant_context(two_churches["grace"]):
        assert db.session.query(Branch).filter_by(is_headquarters=True, is_active=True).count() == 1


def test_plan_branch_limit(client, db, two_churches):
    login(client, "owner@word.test")            # Living Word is on Starter: 1 branch
    res = client.get("/branches/new", follow_redirects=True)
    assert "Your plan allows 1 active branch" in res.get_data(as_text=True)
    client.post("/branches/new", data={"name": "Extra", "code": "EXTRA", "country": "GH"})
    with tenant_context(two_churches["word"]):
        assert db.session.query(Branch).count() == 1


# --------------------------------------------------------------------------- #
# Denominations
# --------------------------------------------------------------------------- #
def test_denomination_member_church_flow(app, client, db):
    register(client, kind="denomination_hq", church_name="Example Gospel Church HQ", email="gs@egc.test")
    assert "Member churches" in client.get("/dashboard").get_data(as_text=True)
    res = client.post("/member-churches/new", data={"name": "Example Gospel Church – Madina", "city": "Madina",
                                                     "admin_first": "Kojo", "admin_last": "Mensah",
                                                     "admin_email": "kojo@egc.test"})
    assert res.status_code == 302
    page = client.get("/member-churches").get_data(as_text=True)
    assert "Example Gospel Church – Madina" in page

    with tenant_bypass():
        hq = db.session.query(Tenant).filter_by(name="Example Gospel Church HQ").one()
        child = db.session.query(Tenant).filter_by(parent_tenant_id=hq.id).one()
        assert child.kind == TenantKind.DENOMINATION_MEMBER and child.denomination_id == hq.denomination_id
        child_branch = db.session.query(Branch).filter_by(tenant_id=child.id).one()

    # Headquarters sees the summary but cannot open the member church's records.
    assert client.get(f"/branches/{child_branch.public_id}/edit").status_code == 404

    mail = mailer.outbox[-1]
    assert mail.to == "kojo@egc.test" and "Madina" in mail.subject
    link = re.search(r"http://localhost(/set-password/\S+)", mail.text).group(1)
    client.post("/logout")

    res = client.post(link, data={"password": "Kojo-Strong-77", "confirm": "Kojo-Strong-77"})
    assert res.status_code == 302 and res.headers["Location"].endswith("/dashboard")
    assert "Example Gospel Church – Madina" in client.get("/dashboard").get_data(as_text=True)
    # Link is single-use.
    client.post("/logout")
    assert client.get(link, follow_redirects=True).get_data(as_text=True).count("expired or has already been used") == 1


def test_member_churches_hidden_for_independent_church(client, two_churches):
    login(client, "owner@grace.test")
    assert client.get("/member-churches").status_code == 404
    assert client.get("/member-churches/new").status_code == 404


# --------------------------------------------------------------------------- #
# Shell
# --------------------------------------------------------------------------- #
def test_dashboard_shell_and_csp(client, two_churches):
    from tests.test_design_system import assert_csp_clean
    login(client, "owner@grace.test")
    for path in ("/dashboard", "/branches", "/branches/new", "/church/settings"):
        html = client.get(path).get_data(as_text=True)
        assert_csp_clean(html)
        assert 'class="tabbar"' in html and 'aria-current="page"' in html, path
        assert "owner@grace.test" in html
    html = client.get("/dashboard").get_data(as_text=True)
    assert 'translate="no">Grace Chapel International' in html


@pytest.mark.parametrize("path", ["/login", "/register", "/churches"])
def test_auth_pages_csp_clean(client, two_churches, path):
    from tests.test_design_system import assert_csp_clean
    if path == "/churches":
        login(client, "owner@grace.test")
    res = client.get(path)
    assert res.status_code == 200
    assert_csp_clean(res.get_data(as_text=True))
