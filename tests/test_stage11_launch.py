"""Stage 11: installable app, encryption at rest, data export, monitoring and background jobs."""
import io
import json
import zipfile
from datetime import date

import pytest

from app.cli import seed_plans
from app.models import AuditLog, CounsellingCase, Member, PrayerRequest, Tenant, TenantUser, User
from app.services import members as member_svc, security
from app.utils.crypto import decrypt, encrypt, is_encrypted
from app.utils.tenancy import tenant_bypass, tenant_context
from tests.test_design_system import assert_csp_clean


def login(client, email, password="Correct-Horse-9"):
    return client.post("/login", data={"email": email, "password": password})


@pytest.fixture(autouse=True)
def _plans(db):
    with tenant_bypass():
        seed_plans()
        db.session.commit()


# --------------------------------------------------------------------------- #
# Installable app
# --------------------------------------------------------------------------- #
def test_manifest_is_installable(client):
    res = client.get("/manifest.webmanifest")
    assert res.mimetype == "application/manifest+json"
    m = json.loads(res.data)
    assert m["display"] == "standalone" and m["start_url"].startswith("/") and m["scope"] == "/"
    sizes = {(i["sizes"], i.get("purpose")) for i in m["icons"]}
    assert ("192x192", "any") in sizes and ("512x512", "any") in sizes and ("512x512", "maskable") in sizes
    for icon in m["icons"]:
        assert client.get(icon["src"]).status_code == 200


def test_service_worker_never_caches_pages(client, app):
    res = client.get("/sw.js")
    js = res.get_data(as_text=True)
    assert res.mimetype == "text/javascript" and res.headers["Service-Worker-Allowed"] == "/"
    assert app.config["APP_VERSION"] in js and "__PRECACHE__" not in js
    assert 'req.mode === "navigate"' in js and "caches.match(OFFLINE)" in js
    assert "cache.put(req" in js and 'url.pathname.indexOf("/static/") === 0' in js


def test_pages_link_manifest_and_offline_page_works(client):
    login_page = client.get("/login").get_data(as_text=True)
    assert 'rel="manifest"' in login_page and "apple-touch-icon" in login_page and 'data-sw="/sw.js"' in login_page
    assert "data-install-app" in login_page
    off = client.get("/offline")
    assert off.status_code == 200 and "You're offline" in off.get_data(as_text=True)
    assert_csp_clean(off.get_data(as_text=True))


def test_public_church_site_is_not_the_app(client, db, two_churches):
    with tenant_context(two_churches["grace"]):
        t = db.session.get(Tenant, two_churches["grace"])
        t.settings = {**(t.settings or {}), "website": {"enabled": True}}
        db.session.commit()
        slug = t.slug
    html = client.get(f"/c/{slug}").get_data(as_text=True)
    assert 'rel="manifest"' not in html and "data-sw" not in html


# --------------------------------------------------------------------------- #
# Encryption at rest
# --------------------------------------------------------------------------- #
def test_two_step_keys_are_encrypted_in_the_database(app, db, two_churches):
    with app.app_context():
        assert decrypt(encrypt("JBSWY3DPEHPK3PXP")) == "JBSWY3DPEHPK3PXP" and decrypt("legacyplain") == "legacyplain"
        with tenant_bypass():
            u = db.session.query(User).filter_by(email="owner@grace.test").one()
            codes = security.enable_two_factor(u, "JBSWY3DPEHPK3PXP") if hasattr(security, "enable_two_factor") else None
            u.totp_secret = "JBSWY3DPEHPK3PXP"
            db.session.commit()
            raw = db.session.execute(db.text("SELECT totp_secret FROM users WHERE email='owner@grace.test'")).scalar()
            assert is_encrypted(raw) and "JBSWY3DPEHPK3PXP" not in raw
            db.session.expire_all()
            assert db.session.query(User).filter_by(email="owner@grace.test").one().totp_secret == "JBSWY3DPEHPK3PXP"


def test_encrypt_secrets_command_converts_plain_values(app, db, two_churches):
    with tenant_bypass():
        db.session.execute(db.text("UPDATE users SET totp_secret='PLAINSECRET' WHERE email='owner@word.test'"))
        db.session.commit()
    out = app.test_cli_runner().invoke(args=["churchplus", "encrypt-secrets"])
    assert "Secrets encrypted: 1" in out.output
    with tenant_bypass():
        raw = db.session.execute(db.text("SELECT totp_secret FROM users WHERE email='owner@word.test'")).scalar()
        assert is_encrypted(raw)
        db.session.expire_all()
        assert db.session.query(User).filter_by(email="owner@word.test").one().totp_secret == "PLAINSECRET"


# --------------------------------------------------------------------------- #
# Data export
# --------------------------------------------------------------------------- #
def test_owner_downloads_church_data_without_confidential_records(client, db, two_churches):
    tid = two_churches["grace"]
    with tenant_context(tid):
        t = db.session.get(Tenant, tid)
        m = member_svc.create_member(t, None, first_name="Ama", last_name="Boateng", phone="0244000001")
        owner = db.session.query(TenantUser).first()
        db.session.add(PrayerRequest(name="Open", request="OPEN-PRAYER", is_confidential=False))
        db.session.add(PrayerRequest(name="Secret", request="SECRET-PRAYER", is_confidential=True))
        db.session.add(CounsellingCase(reference="CC-00077", member_id=m.id, category="Grief", counsellor_id=owner.id,
                                       opened_on=date.today()))
        db.session.commit()
    login(client, "owner@grace.test")
    res = client.post("/church/export")
    assert res.mimetype == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(res.data))
    names = set(z.namelist())
    assert {"members.csv", "contributions.csv", "journal_lines.csv", "README.txt"} <= names
    everything = "".join(z.read(n).decode() for n in names)
    assert "Ama" in everything and "Boateng" in everything
    for secret in ("SECRET-PRAYER", "OPEN-PRAYER", "CC-00077", "Grief", "Secret"):
        assert secret not in everything
    with tenant_context(tid):
        assert db.session.query(AuditLog).filter_by(action="church.data_exported").count() == 1


def test_only_the_owner_can_download_everything(client, db, two_churches):
    from app.models import Role
    from tests.conftest import make_user
    tid = two_churches["grace"]
    with tenant_context(tid):
        role = db.session.query(Role).filter_by(slug="church_admin").one()
        u = make_user("admin@grace.test")
        with tenant_bypass():
            db.session.add(u)
            db.session.flush()
        db.session.add(TenantUser(user_id=u.id, roles=[role]))
        db.session.commit()
    login(client, "admin@grace.test")
    assert client.post("/church/export").status_code == 403


def test_export_church_command(app, db, two_churches, tmp_path):
    with tenant_bypass():
        slug = db.session.get(Tenant, two_churches["word"]).slug
    out = tmp_path / "word.zip"
    res = app.test_cli_runner().invoke(args=["churchplus", "export-church", "--slug", slug, "--out", str(out)])
    assert res.exit_code == 0 and zipfile.is_zipfile(out)


# --------------------------------------------------------------------------- #
# Monitoring and jobs
# --------------------------------------------------------------------------- #
def test_every_response_has_a_request_reference(client):
    res = client.get("/healthz")
    assert len(res.headers["X-Request-ID"]) >= 8
    mine = client.get("/healthz", headers={"X-Request-ID": "abc12345-trace"})
    assert mine.headers["X-Request-ID"] == "abc12345-trace"
    bad = client.get("/healthz", headers={"X-Request-ID": "<script>"})
    assert bad.headers["X-Request-ID"] != "<script>"


def test_server_error_shows_reference(app, client):
    @app.get("/boom-test")
    def boom():
        raise RuntimeError("kaboom")
    app.config["PROPAGATE_EXCEPTIONS"] = False
    res = client.get("/boom-test", headers={"X-Request-ID": "ref-12345678"})
    assert res.status_code == 500 and "ref-12345678" in res.get_data(as_text=True)


@pytest.mark.parametrize("cmd", ["send-due", "send-reminders", "billing-run"])
def test_background_job_commands_run(app, db, two_churches, cmd):
    res = app.test_cli_runner().invoke(args=["churchplus", cmd])
    assert res.exit_code == 0, res.output


def test_seed_demo_creates_portal_and_platform_logins(app, db):
    res = app.test_cli_runner().invoke(args=["churchplus", "seed-demo"])
    assert res.exit_code == 0, res.output
    with tenant_bypass():
        assert db.session.query(User).filter_by(email="admin@churchplus.test").one().is_platform_staff
        portal = db.session.query(User).filter_by(email="abena@gracechapel.test").one()
        acc = db.session.query(TenantUser).filter_by(user_id=portal.id).one()
        assert acc.is_portal_only
